"""Agnes Image core + CLI. Existing generate(...) returns (path, elapsed).

CLI: agnes_image.py "prompt" --style ink --image photo.jpg --count 2
     agnes_image.py --history
     agnes_image.py --retry JOB_ID
No automatic POST retries. Multi-image batches use sequential single requests.
"""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "models.json")
API_CONFIG_PATH = os.path.join(TOOLS_DIR, "api_config.json")
STYLES_PATH = os.path.join(TOOLS_DIR, "styles.json")
DEFAULT_OUT_DIR = os.path.normpath(os.path.join(TOOLS_DIR, "..", "outputs"))
MODEL_ID = "agnes-image-2.1-flash"
CHAT_MODEL_ID = "agnes-2.5-flash"
IMAGE_URL_DEFAULT = "https://apihub.agnes-ai.com/v1/images/generations"
SIZES = ["1K", "2K", "3K", "4K"]
RATIOS = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_PROMPT = 8000
MAX_COUNT = 4
_LOCAL_LOCK = threading.Lock()


class GenError(Exception):
    def __init__(self, message, stage="validation", retryable=False, item=None):
        super().__init__(message)
        self.stage, self.retryable, self.item = stage, retryable, item


def _key(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m[1], ""), value).strip()
    return value if value and not re.search(r"\s|\$\{", value) else ""


def _endpoint(value):
    if not isinstance(value, str):
        raise GenError("接口地址格式不正确。", "config")
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
        raise GenError("生图接口必须使用有效 HTTPS 地址。", "config")
    return value


def mask_key(value):
    """Never expose a full key in logs, records or the UI."""
    if not value:
        return ""
    return value if len(value) <= 10 else "%s…%s" % (value[:6], value[-4:])


def load_api_config():
    """Tool-owned Agnes credential; takes priority over everything else."""
    try:
        with open(API_CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise GenError("API 配置文件无法读取或 JSON 格式错误，请检查本目录下的 api_config.json。", "config") from None
    return cfg if isinstance(cfg, dict) else {}


def get_saved_api_key():
    return _key(load_api_config().get("apiKey"))


def save_api_config(api_key, url=None):
    """Persist the user's own Agnes key until they change it again."""
    key = _key(api_key)
    if not key:
        raise GenError("API Key 不能为空，且不能包含空格或未解析的 ${} 占位符。", "config")
    data = {"apiKey": key}
    if url:
        data["url"] = _endpoint(url)
    tmp = API_CONFIG_PATH + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, API_CONFIG_PATH)
    except OSError:
        raise GenError("API 配置保存失败，请检查本目录写入权限。", "config") from None
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return data


def clear_api_config():
    """Fall back to AGNES_API_KEY / models.json once the saved key is removed."""
    try:
        os.remove(API_CONFIG_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        raise GenError("API 配置删除失败，请检查本目录写入权限。", "config") from None


def active_credential():
    """Describe which key would actually be used right now, without leaking it."""
    saved = get_saved_api_key()
    if saved:
        cfg = load_api_config()
        return {"apiKey": saved, "url": _endpoint(cfg.get("url") or IMAGE_URL_DEFAULT), "source": "tool"}
    env_key = _key(os.environ.get("AGNES_API_KEY"))
    if env_key:
        return {"apiKey": env_key, "url": IMAGE_URL_DEFAULT, "source": "env"}
    try:
        return dict(load_model(), source="models")
    except GenError:
        return {"apiKey": "", "url": IMAGE_URL_DEFAULT, "source": "none"}


def test_api_key(api_key=None, url=None, timeout=45):
    """Credential probe: posts a deliberately invalid size.

    Agnes validates auth before parameters, so 400 means "key accepted" while
    401/403 means "key rejected" — and no image is ever generated or billed.
    """
    key = _key(api_key) or get_saved_api_key() or active_credential().get("apiKey")
    if not key:
        raise GenError("当前没有任何可用的 Agnes Key，请先填写并保存。", "config")
    target = _endpoint(url) if url else _endpoint(load_api_config().get("url") or IMAGE_URL_DEFAULT)
    body = {"model": MODEL_ID, "prompt": "key-check", "size": "__invalid__", "ratio": "1:1"}
    req = urllib.request.Request(target, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as r:
            _read_response(r, 8 * 1024 * 1024)
        return "连接正常：Key 已被 Agnes 接受，可调用生图服务。"
    except urllib.error.HTTPError as e:
        detail = _server_message(e)
        if e.code in (401, 403):
            raise GenError("Key 校验失败：HTTP %s %s" % (e.code, detail or "Key 无效或已过期")) from None
        if e.code == 400 or 400 <= e.code < 500:
            return "连接正常：Key 有效（服务端已接受请求，仅测试参数被拒）。"
        raise GenError("服务返回 HTTP %s %s" % (e.code, detail or ""), "config", True) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise GenError("无法连接 Agnes 服务，请检查网络或接口地址。", "config", True) from None


def load_model():
    """Read credentials only; never modify or copy the user's models.json."""
    saved = get_saved_api_key()
    if saved:
        url = load_api_config().get("url") or IMAGE_URL_DEFAULT
        return {"url": _endpoint(url), "apiKey": saved}
    env_key = _key(os.environ.get("AGNES_API_KEY"))
    if env_key:
        return {"url": IMAGE_URL_DEFAULT, "apiKey": env_key}
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = []
    except (OSError, ValueError):
        raise GenError("模型配置无法读取或 JSON 格式错误，请检查 models.json。", "config") from None
    models = cfg if isinstance(cfg, list) else cfg.get("models", []) if isinstance(cfg, dict) else None
    if not isinstance(models, list):
        raise GenError("模型配置必须为数组或包含 models 数组。", "config")
    for model_id in (MODEL_ID, CHAT_MODEL_ID):
        for m in models:
            if isinstance(m, dict) and m.get("id") == model_id and _key(m.get("apiKey")):
                url = m.get("url") if model_id == MODEL_ID else IMAGE_URL_DEFAULT
                return {"url": _endpoint(url or IMAGE_URL_DEFAULT), "apiKey": _key(m["apiKey"])}
    raise GenError("未找到有效 Agnes Key：请设置 AGNES_API_KEY，或配置 Agnes 对话模型的 Key。无需添加生图模型到聊天列表。", "config")


def load_styles():
    fallback = [{"id": "none", "name": "不加风格（原样）", "suffix": ""}]
    try:
        with open(STYLES_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return fallback, {"size": "1K", "ratio": "16:9", "style": "none"}
    except (OSError, ValueError):
        raise GenError("styles.json 无法读取或格式错误。", "config") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("styles"), list):
        raise GenError("风格配置需要 styles 数组。", "config")
    styles, ids = [], set()
    for s in cfg["styles"]:
        if (not isinstance(s, dict) or not all(isinstance(s.get(k), str) for k in ("id", "name", "suffix"))
                or not s["id"] or s["id"] in ids):
            raise GenError("风格配置包含缺失字段或重复 id。", "config")
        ids.add(s["id"])
        styles.append(s)
    defaults = cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        raise GenError("风格 defaults 必须为对象。", "config")
    if not styles:
        styles = fallback
    defaults = dict(defaults)
    defaults["size"] = defaults.get("size") if defaults.get("size") in SIZES else "1K"
    defaults["ratio"] = defaults.get("ratio") if defaults.get("ratio") in RATIOS else "16:9"
    defaults["style"] = defaults.get("style") if defaults.get("style") in {s["id"] for s in styles} else styles[0]["id"]
    return styles, defaults


def find_style(style_id, styles):
    for s in styles:
        if s["id"] == style_id:
            return s
    raise GenError("未知风格：%s。请刷新风格库或重新选择。" % style_id)


def build_prompt(text, style_id=None, styles=None):
    text = (text or "").strip()
    if not style_id:
        return text
    if styles is None:
        styles, _ = load_styles()
    suffix = find_style(style_id, styles)["suffix"].strip()
    return (text.rstrip("，,。. ") + "，" + suffix) if suffix and text else (text or suffix)


def slugify(text, limit=24):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", text or "image").strip("_")[:limit] or "image"


def default_out_path(prompt):
    return os.path.join(DEFAULT_OUT_DIR, "%s_%s_%s.png" % (slugify(prompt), time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8]))


def image_info(data, max_pixels=MAX_PIXELS):
    """Magic + dimension checks without third-party dependency; Pillow verifies when available."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            if b"IEND" not in data[-32:] or b"acTL" in data:
                raise ValueError()
            fmt, mime, ext = "PNG", "image/png", ".png"
        elif data.startswith(b"\xff\xd8"):
            pos, w, h = 2, 0, 0
            while pos < len(data):
                if data[pos] != 255:
                    raise ValueError()
                while pos < len(data) and data[pos] == 255:
                    pos += 1
                marker = data[pos]
                pos += 1
                if marker in (0xD9, 0xDA):
                    break
                if marker in (0x01, *range(0xD0, 0xD9)):
                    continue
                length = int.from_bytes(data[pos:pos+2], "big")
                if length < 2 or pos + length > len(data):
                    raise ValueError()
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[pos+3:pos+7])
                    break
                pos += length
            if not w or b"\xff\xd9" not in data[-32:]:
                raise ValueError()
            fmt, mime, ext = "JPEG", "image/jpeg", ".jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if int.from_bytes(data[4:8], "little") + 8 != len(data):
                raise ValueError()
            chunk = data[12:16]
            if chunk == b"VP8X":
                if data[20] & 2:
                    raise ValueError()
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
            elif chunk == b"VP8L" and data[20] == 0x2F:
                n = int.from_bytes(data[21:25], "little")
                w, h = (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
            elif chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                w, h = w & 0x3FFF, h & 0x3FFF
            else:
                raise ValueError()
            fmt, mime, ext = "WEBP", "image/webp", ".webp"
        else:
            raise ValueError()
        if not w or not h or w * h > max_pixels:
            raise GenError("图片像素数超过本工具安全限制（%d 万像素）。" % (max_pixels // 10000))
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image:
            with Image.open(io.BytesIO(data)) as im:
                if getattr(im, "n_frames", 1) != 1:
                    raise ValueError()
                im.verify()
        return {"width": w, "height": h, "format": fmt, "mime": mime, "ext": ext, "bytes": len(data)}
    except GenError:
        raise
    except Exception:
        raise GenError("图片损坏或格式不支持；请选择静态 PNG、JPEG 或 WebP。") from None


def read_reference(path):
    try:
        p = Path(path).expanduser().resolve(strict=True)
        if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise GenError("参考图仅支持 PNG、JPG/JPEG、WebP 静态图片。")
        with p.open("rb") as f:
            data = f.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise GenError("参考图不能为空且不得超过 10 MB（本工具限制）。")
    except (OSError, ValueError, TypeError):
        raise GenError("参考图不存在或不可读取，请重新选择。") from None
    info = image_info(data)
    info.update(path=str(p), sha256=hashlib.sha256(data).hexdigest())
    return data, info


def validate_request(prompt, size, ratio, count=1, timeout=300, image=None):
    if not isinstance(prompt, str) or not prompt.strip():
        raise GenError("请填写提示词；不能仅依赖风格后缀。")
    if len(prompt) > MAX_PROMPT:
        raise GenError("最终提示词不能超过 %d 个字符。" % MAX_PROMPT)
    if size not in SIZES or ratio not in RATIOS:
        raise GenError("请选择有效的尺寸档位和比例。")
    if type(count) is not int or not 1 <= count <= MAX_COUNT:
        raise GenError("生成数量须为 1–4 的整数；将逐张提交。")
    if type(timeout) not in (int, float) or not 30 <= timeout <= 600:
        raise GenError("超时须为 30–600 秒。")
    return read_reference(image)[1] if image else None


def _server_message(error):
    """Surface the real upstream reason instead of a bare 'service error'."""
    try:
        raw = error.read(4096)
        payload = json.loads(raw)
    except Exception:
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        text = err.get("message") or err.get("code") or ""
    else:
        text = err if isinstance(err, str) else (payload.get("message") if isinstance(payload, dict) else "")
    return re.sub(r"\s+", " ", str(text)).strip()[:300]


def _http_error(code, stage, detail=None):
    messages = {400: "请求参数被拒绝，请检查模型、提示词和参考图。", 401: "API Key 无效或已过期。",
                403: "当前 Key 无权限使用该模型。", 404: "接口地址或模型不存在。",
                413: "服务端拒绝过大的请求，请压缩参考图。", 429: "服务限流或额度不足，请稍后手动重试。"}
    message = messages.get(code, "服务暂时异常，请稍后手动重试。" if code >= 500 else "服务拒绝请求，请检查配置。")
    if detail:
        message += " 服务端返回：%s" % detail
    return GenError("HTTP %s：%s" % (code, message), stage, code in (408, 429) or code >= 500)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_response(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise GenError("服务响应超过本工具大小限制。", "response")
    return data


def request_image(prompt, size, ratio, timeout, reference=None):
    m = load_model()
    body = {"model": MODEL_ID, "prompt": prompt, "size": size, "ratio": ratio,
            "extra_body": {"response_format": "url"}}
    if reference:
        data, info = reference
        body["extra_body"]["image"] = ["data:%s;base64,%s" % (info["mime"], base64.b64encode(data).decode("ascii"))]
    req = urllib.request.Request(m["url"], data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + m["apiKey"], "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as r:
            raw = _read_response(r, 90 * 1024 * 1024)
        payload = json.loads(raw)
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ValueError()
        item = items[0]
        if not (isinstance(item.get("url"), str) and item["url"] or isinstance(item.get("b64_json"), str) and item["b64_json"]):
            raise ValueError()
        return {k: item[k] for k in ("url", "b64_json") if item.get(k)}
    except urllib.error.HTTPError as e:
        raise _http_error(e.code, "submit", _server_message(e)) from None
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        raise GenError("请求超时或网络中断；服务端可能已生成。不会自动重复提交，请确认后手动重试。", "submit", True) from None
    except (ValueError, TypeError, KeyError):
        raise GenError("服务响应不是有效的生图结果；未保存任何伪图片。", "response", True) from None


def download_item(item, out_path):
    try:
        if item.get("url"):
            url = _endpoint(item["url"])
            with urllib.request.build_opener(_NoRedirect()).open(url, timeout=180) as r:
                data = _read_response(r, MAX_RESULT_BYTES)
        elif item.get("b64_json"):
            if len(item["b64_json"]) > MAX_RESULT_BYTES * 4 // 3 + 8:
                raise GenError("返回的图片过大。", "download")
            data = base64.b64decode(item["b64_json"], validate=True)
        else:
            raise GenError("响应中没有图片地址或内容。", "download")
        info = image_info(data, 64_000_000)
        path = Path(out_path).resolve().with_suffix(info["ext"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents overwriting any pre-existing output.
        with path.open("xb") as f:
            try:
                f.write(data)
            except BaseException:
                f.close()
                path.unlink(missing_ok=True)
                raise
        return str(path)
    except urllib.error.HTTPError as e:
        err = _http_error(e.code, "download", _server_message(e))
        err.retryable, err.item = True, item
        raise err from None
    except GenError as e:
        raise GenError(str(e), "download", True, item) from None
    except (OSError, ValueError):
        raise GenError("图片下载、校验或保存失败。检查网络、磁盘权限或是否已有同名文件；可重试下载而不重新生成。", "download", True, item) from None


def generate(prompt, size="1K", ratio="16:9", out_path=None, timeout=300, on_progress=None, image=None):
    """Legacy single-image entry; same signature and return type as before."""
    validate_request(prompt, size, ratio, timeout=timeout)
    reference = read_reference(image) if image else None
    started = time.monotonic()
    if on_progress:
        on_progress("正在提交并等待生成结果…")
    item = request_image(prompt.strip(), size, ratio, timeout, reference)
    if on_progress:
        on_progress("正在下载并校验图片…")
    path = download_item(item, out_path or default_out_path(prompt))
    return path, time.monotonic() - started


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _records(out_dir):
    return Path(out_dir) / "records"


def _record_path(out_dir, job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise GenError("记录编号无效。")
    return _records(out_dir) / (job_id + ".json")


def save_record(record, out_dir):
    path = _record_path(out_dir, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_record(job_id, out_dir=DEFAULT_OUT_DIR):
    try:
        with _record_path(out_dir, job_id).open(encoding="utf-8") as f:
            r = json.load(f)
        if r["id"] != job_id or r["version"] != 1 or not isinstance(r["params"], dict) or not isinstance(r["items"], list):
            raise ValueError()
        return r
    except (OSError, ValueError, KeyError, TypeError):
        raise GenError("生成记录不存在或损坏。") from None


def list_records(out_dir=DEFAULT_OUT_DIR):
    records, warnings = [], []
    for p in _records(out_dir).glob("*.json"):
        try:
            records.append(load_record(p.stem, out_dir))
        except GenError:
            warnings.append(p.name)
    return sorted(records, key=lambda r: r.get("created", ""), reverse=True), warnings


@contextlib.contextmanager
def generation_lock(out_dir):
    """OS lock is automatically released on crash; covers CLI and all GUI windows."""
    if not _LOCAL_LOCK.acquire(blocking=False):
        raise GenError("已有生成任务正在执行，请等待完成。", "busy")
    f, locked = None, False
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        f = open(Path(out_dir) / ".generation.lock", "a+b")
        if f.seek(0, 2) == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise GenError("另一个窗口或命令行正在生成，请稍后再试。", "busy") from None
        yield
    finally:
        if f:
            if locked:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()
        _LOCAL_LOCK.release()


def run_job(prompt=None, style_id=None, size="1K", ratio="16:9", count=1, image=None,
            timeout=300, out_dir=DEFAULT_OUT_DIR, on_event=None, stop_event=None,
            retry_id=None, out_path=None):
    """Persisted batch runner; retry downloads known results without another POST."""
    def emit(kind, value):
        if on_event:
            on_event(kind, value)
    with generation_lock(out_dir):
        if retry_id:
            record = load_record(retry_id, out_dir)
            params = record["params"]
        else:
            validate_request(prompt, size, ratio, count, timeout)
            final = build_prompt(prompt, style_id)
            validate_request(final, size, ratio, count, timeout)
            if out_path and count != 1:
                raise GenError("--out 仅限单张生成；多张请使用默认输出目录。")
            params = {"prompt": prompt.strip(), "final_prompt": final, "style": style_id,
                      "size": size, "ratio": ratio, "count": count, "timeout": timeout,
                      "mode": "image" if image else "text", "image": str(Path(image).resolve()) if image else None,
                      "reference_sha256": None}
            record = {"version": 1, "id": uuid.uuid4().hex, "created": _now(), "status": "pending",
                      "params": params, "items": [{"index": i+1, "status": "pending", "attempts": 0,
                      "out_path": str(Path(out_path).resolve()) if out_path else None} for i in range(count)]}
        pending = [i for i in record["items"] if i["status"] != "success"]
        if not pending:
            return record
        validate_request(params["final_prompt"], params["size"], params["ratio"], params["count"], params["timeout"])
        needs_post = any(not item.get("source") for item in pending)
        reference = None
        if needs_post:
            load_model()
            if params.get("image"):
                reference = read_reference(params["image"])
                digest = reference[1]["sha256"]
                if params.get("reference_sha256") not in (None, digest):
                    raise GenError("参考图已被修改。为保证参数一致，请复用参数并重新选择参考图创建新任务。")
                params["reference_sha256"] = digest
        record["status"], record["updated"] = "running", _now()
        save_record(record, out_dir)
        emit("record", record["id"])
        for item in pending:
            if stop_event and stop_event.is_set():
                break
            item.update(status="running", error=None, attempts=item["attempts"] + 1)
            record["updated"] = _now()
            save_record(record, out_dir)
            started = time.monotonic()
            try:
                source = item.get("source")
                if not source:
                    emit("status", "第 %d/%d 张：已提交，等待生成（无真实百分比）" % (item["index"], params["count"]))
                    source = request_image(params["final_prompt"], params["size"], params["ratio"], params["timeout"], reference)
                    item["source"] = source
                    save_record(record, out_dir)
                emit("status", "第 %d/%d 张：下载并校验结果" % (item["index"], params["count"]))
                target = item.get("out_path") or str(Path(out_dir) / (record["id"] + "_%02d.png" % item["index"]))
                path = download_item(source, target)
                item.update(status="success", path=path, stage="done", retryable=False)
                item.pop("source", None)
                emit("result", path)
            except GenError as e:
                item.update(status="failed", error=str(e), stage=e.stage, retryable=e.retryable)
                emit("status", str(e))
            except OSError:
                item.update(status="failed", error="本地存储不可用，请检查磁盘和权限。", stage="storage", retryable=True)
            item["elapsed"] = round(time.monotonic() - started, 2)
            record["updated"] = _now()
            save_record(record, out_dir)
            emit("progress", (sum(x["status"] == "success" for x in record["items"]), params["count"]))
            if item["status"] != "success":
                # Avoid repeated rejection/quota usage; remaining images wait for explicit retry.
                break
        success = sum(i["status"] == "success" for i in record["items"])
        record["status"] = "success" if success == params["count"] else "partial" if success else "failed"
        if stop_event and stop_event.is_set() and success < params["count"]:
            record["status"] = "stopped"
        record["updated"] = _now()
        save_record(record, out_dir)
        emit("done", record)
        return record


def main():
    try:
        styles, defaults = load_styles()
        ap = argparse.ArgumentParser(description="Agnes 生图：文生图 / 图生图，支持记录与手动重试")
        ap.add_argument("prompt", nargs="?")
        ap.add_argument("--style", "-s")
        ap.add_argument("--size", choices=SIZES, default=defaults["size"])
        ap.add_argument("--ratio", choices=RATIOS, default=defaults["ratio"])
        ap.add_argument("--count", type=int, default=1)
        ap.add_argument("--out")
        ap.add_argument("--timeout", type=int, default=300)
        ap.add_argument("--image")
        ap.add_argument("--list-styles", action="store_true")
        ap.add_argument("--history", action="store_true")
        ap.add_argument("--set-key", metavar="KEY", help="保存自己的 Agnes API Key（一直生效，直到再次修改）")
        ap.add_argument("--api-url", help="可选：自定义生图接口地址（HTTPS）")
        ap.add_argument("--clear-key", action="store_true", help="删除已保存的 Key，回退到环境变量或 models.json")
        ap.add_argument("--show-key", action="store_true", help="显示当前生效的 Key 来源（掩码）")
        ap.add_argument("--retry", metavar="JOB_ID", help="显式重试未完成项；可能产生额外费用")
        args = ap.parse_args()
        if args.list_styles:
            for s in styles:
                print("%-12s %s\n             %s" % (s["id"], s["name"], s["suffix"]))
            return 0
        if args.set_key:
            save_api_config(args.set_key, args.api_url)
            print("已保存 API Key：%s（接口：%s）" % (mask_key(_key(args.set_key)), args.api_url or IMAGE_URL_DEFAULT))
            return 0
        if args.clear_key:
            clear_api_config()
            print("已删除保存的 API Key，将回退到 AGNES_API_KEY 或 models.json。")
            return 0
        if args.show_key:
            cred = active_credential()
            names = {"tool": "本工具保存的 Key", "env": "环境变量 AGNES_API_KEY",
                     "models": "models.json 中的 Agnes 模型 Key", "none": "未找到可用 Key"}
            print("%s：%s" % (names[cred["source"]], mask_key(cred["apiKey"])))
            print("接口地址：%s" % cred["url"])
            return 0
        if args.history:
            records, warnings = list_records()
            for r in records:
                print(r["id"], r["created"], r["status"], r["params"]["prompt"][:40])
            if warnings:
                print("已跳过 %d 个损坏记录。" % len(warnings), file=sys.stderr)
            return 0
        if not args.prompt and not args.retry:
            ap.print_help()
            return 2
        def event(kind, value):
            if kind in ("status", "record", "result"):
                print(value, flush=True)
        record = run_job(args.prompt, args.style or defaults["style"], args.size, args.ratio,
                         args.count, args.image, args.timeout, on_event=event,
                         retry_id=args.retry, out_path=args.out)
        print("任务 %s：%s" % (record["id"], record["status"]))
        return 0 if record["status"] == "success" else 1
    except (GenError, OSError) as e:
        message = str(e) if isinstance(e, GenError) else "本地文件操作失败，请检查权限和磁盘空间。"
        print("出错：" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
