"""服务器桌面入口：只读检测连接，并在默认浏览器打开同一站点。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import queue
import socket
import ssl
import threading
import tkinter as tk
import tkinter.ttk as ttk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from launcher.desktop_config import resolve_desktop_config
from launcher.gui_theme import COLORS, make_card, make_header, setup_styles


PROBE_TIMEOUT = 8.0
MAX_STATUS_BYTES = 64 * 1024


@dataclass(frozen=True)
class RemoteServerStatus:
    """连接检测结果，不包含账号、令牌或其他身份信息。"""

    connected: bool
    message: str
    detail: str = ""
    degraded: bool = False
    http_status: int | None = None


def _server_origin(base_url: str) -> str:
    """复用启动配置校验，远程入口不能误连接到本地运行模式。"""
    config = resolve_desktop_config(base_url)
    if config.mode != "remote":
        raise ValueError("此入口仅用于服务器模式，本地运行请使用本地启动器")
    return config.base_url


class _CrossOriginRedirect(urllib.error.URLError):
    pass


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """检测请求不随重定向访问其他站点。"""

    def __init__(self, origin: str):
        super().__init__()
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, new_url):
        destination = urllib.parse.urlsplit(new_url)
        try:
            redirected_origin = _server_origin(urllib.parse.urlunsplit((destination.scheme, destination.netloc, "", "", "")))
        except ValueError as error:
            if response is not None:
                response.close()
            raise _CrossOriginRedirect("服务器地址发生跳转") from error
        if redirected_origin != self.origin:
            if response is not None:
                response.close()
            raise _CrossOriginRedirect("服务器地址发生跳转")
        return super().redirect_request(request, response, code, message, headers, new_url)


def probe_remote_server(base_url: str, *, timeout: float = PROBE_TIMEOUT) -> RemoteServerStatus:
    """只读 GET 网站公开状态接口，有限超时与响应大小，不携带登录凭据。"""
    try:
        origin = _server_origin(base_url)
    except ValueError as error:
        return RemoteServerStatus(False, "服务器地址不正确", str(error))
    request = urllib.request.Request(
        f"{origin}/api/v1/public/status",
        method="GET",
        headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "XianyuDesktop/1.0"},
    )
    try:
        opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin))
        with opener.open(request, timeout=timeout) as response:
            http_status = response.getcode()
            raw = response.read(MAX_STATUS_BYTES + 1)
        if http_status != 200:
            return RemoteServerStatus(False, "服务器暂时无法连接", f"状态接口返回 HTTP {http_status}，请稍后重试。", http_status=http_status)
        if len(raw) > MAX_STATUS_BYTES:
            return RemoteServerStatus(False, "状态接口返回异常", "响应内容过大，请确认配置的是本系统网站地址。", http_status=http_status)
        payload = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(payload, dict) or type(payload.get("success")) is not bool:
            return RemoteServerStatus(False, "状态接口返回异常", "请确认配置的是本系统网站地址。", http_status=http_status)
        if not payload["success"]:
            message = payload.get("message")
            detail = " ".join(message.split())[:180] if isinstance(message, str) and message.strip() else "请稍后重新检测，也可以打开网页查看。"
            return RemoteServerStatus(False, "服务器已响应，状态暂不可用", detail, http_status=http_status)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("services"), list) or not data["services"]:
            return RemoteServerStatus(False, "状态接口返回异常", "服务返回了无法识别的状态数据，请稍后重试。", http_status=http_status)
        services = data["services"]
        if any(not isinstance(service, dict) or type(service.get("online")) is not bool for service in services):
            return RemoteServerStatus(False, "状态接口返回异常", "服务返回了无法识别的状态数据，请稍后重试。", http_status=http_status)
        degraded = any(not service["online"] for service in services)
        return RemoteServerStatus(
            True,
            "已连接服务器",
            "网站已响应，部分服务暂不可用；可打开后台查看详情。" if degraded else "服务器状态正常，可以打开管理后台。",
            degraded=degraded,
            http_status=http_status,
        )
    except _CrossOriginRedirect:
        return RemoteServerStatus(False, "服务器地址发生跳转", "请将桌面配置更新为网站最终访问地址后重试。")
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in {401, 403}:
            detail = "状态检测被服务器拒绝，可以尝试打开网页查看。"
        elif error.code == 404:
            detail = "未找到状态接口，请确认配置的是本系统网站地址。"
        elif error.code == 429:
            detail = "检测过于频繁，请稍后重试。"
        else:
            detail = f"状态接口返回 HTTP {error.code}，请稍后重试。"
        return RemoteServerStatus(False, "服务器暂时无法连接", detail, http_status=error.code)
    except (socket.timeout, TimeoutError):
        return RemoteServerStatus(False, "连接超时", "请检查网络连接，稍后点击“检测连接”重试。")
    except ssl.SSLCertVerificationError:
        return RemoteServerStatus(False, "HTTPS 证书验证失败", "请检查服务器的 HTTPS 证书配置。")
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            return RemoteServerStatus(False, "连接超时", "请检查网络连接，稍后点击“检测连接”重试。")
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            return RemoteServerStatus(False, "HTTPS 证书验证失败", "请检查服务器的 HTTPS 证书配置。")
        return RemoteServerStatus(False, "暂时无法连接服务器", "请检查网络和服务器地址，稍后重试。")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return RemoteServerStatus(False, "状态接口返回异常", "返回内容不是有效状态数据，请确认配置的是本系统网站地址。")
    except (OSError, ValueError):
        return RemoteServerStatus(False, "暂时无法连接服务器", "请检查网络和服务器地址，稍后重试。")


def open_remote_site(base_url: str) -> bool:
    """通过默认浏览器打开经过校验的同一网站；不保存或提交账号信息。"""
    return bool(webbrowser.open(_server_origin(base_url), new=2))


def _probe_worker(base_url: str, outbox: queue.Queue) -> None:
    try:
        result = probe_remote_server(base_url)
    except Exception:
        result = RemoteServerStatus(False, "连接检测未完成", "请稍后点击“检测连接”重试。")
    outbox.put(("probe", result))


def _browser_worker(base_url: str, outbox: queue.Queue) -> None:
    try:
        opened = open_remote_site(base_url)
    except Exception:
        opened = False
    outbox.put(("browser", opened))


class RemoteLauncherApp:
    """独立 Tk 外壳；后台线程只写队列，所有控件访问都在主线程完成。"""

    WINDOW_WIDTH = 760
    WINDOW_HEIGHT = 550
    POLL_MILLISECONDS = 100

    def __init__(self, base_url: str, *, root: tk.Tk | None = None, auto_probe: bool = True):
        self.base_url = _server_origin(base_url)
        self.root = root if root is not None else tk.Tk()
        self._closed = False
        self._probe_busy = False
        self._browser_busy = False
        self._outbox: queue.Queue = queue.Queue()
        self._poll_id: str | None = None
        self._probe_thread: threading.Thread | None = None
        self._browser_thread: threading.Thread | None = None
        self.root.title("闲鱼管理桌面端 · 服务器模式")
        self.root.configure(bg=COLORS["bg"])
        self.root.minsize(640, 520)
        self.root.resizable(True, True)
        left = max(0, (self.root.winfo_screenwidth() - self.WINDOW_WIDTH) // 2)
        top = max(0, (self.root.winfo_screenheight() - self.WINDOW_HEIGHT) // 2)
        self.root.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{left}+{top}")
        setup_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._poll_id = self.root.after(self.POLL_MILLISECONDS, self._poll_results)
        if auto_probe:
            self.check_connection()

    def _build_ui(self) -> None:
        container = tk.Frame(self.root, bg=COLORS["bg"])
        container.pack(fill=tk.BOTH, expand=True)
        make_header(container, "服务器工作台", "连接服务器，继续管理订单与消息")

        site_card = make_card(container)
        tk.Label(site_card, text="当前服务器", font=("微软雅黑", 11, "bold"), fg=COLORS["text"], bg=COLORS["card_bg"]).pack(anchor=tk.W)
        self._site_var = tk.StringVar(master=self.root, value=self.base_url)
        ttk.Entry(site_card, textvariable=self._site_var, state="readonly", font=("Consolas", 12)).pack(fill=tk.X, pady=(10, 10))
        tk.Label(site_card, text="使用与网页相同的账号登录，账号、订单和门票资料保持一致。", font=("微软雅黑", 11), fg=COLORS["text_secondary"], bg=COLORS["card_bg"], anchor=tk.W, justify=tk.LEFT, wraplength=580).pack(fill=tk.X)

        status_card = make_card(container)
        self._status_label = tk.Label(status_card, text="尚未检测连接", font=("微软雅黑", 12, "bold"), fg=COLORS["text_secondary"], bg=COLORS["card_bg"], anchor=tk.W)
        self._status_label.pack(fill=tk.X)
        self._status_detail = tk.Label(status_card, text="点击“检测连接”查看服务器状态，也可直接打开后台。", font=("微软雅黑", 10), fg=COLORS["text_secondary"], bg=COLORS["card_bg"], anchor=tk.W, justify=tk.LEFT, wraplength=580)
        self._status_detail.pack(fill=tk.X, pady=(7, 2))

        buttons = tk.Frame(container, bg=COLORS["bg"])
        buttons.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._open_button = ttk.Button(buttons, text="打开管理后台", style="Primary.TButton", command=self.open_site)
        self._open_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self._probe_button = ttk.Button(buttons, text="检测连接", style="Normal.TButton", command=self.check_connection)
        self._probe_button.pack(side=tk.LEFT, padx=(0, 10), ipady=2)
        ttk.Button(buttons, text="退出", style="Normal.TButton", command=self.close).pack(side=tk.LEFT, ipady=2)

        self._browser_detail = tk.Label(container, text="管理后台将在默认浏览器打开。", font=("微软雅黑", 10), fg=COLORS["text_secondary"], bg=COLORS["bg"], anchor=tk.W, justify=tk.LEFT, wraplength=580)
        self._browser_detail.pack(fill=tk.X, padx=22, pady=(12, 0))
        tk.Label(container, text="关闭此窗口仅退出桌面入口，服务器和在线业务会继续运行。", font=("微软雅黑", 10), fg=COLORS["text_secondary"], bg=COLORS["bg"], anchor=tk.W, justify=tk.LEFT, wraplength=580).pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(18, 22))

    def check_connection(self) -> None:
        if self._closed or self._probe_busy:
            return
        self._probe_busy = True
        self._probe_button.configure(state=tk.DISABLED, text="检测中…")
        self._status_label.configure(text="正在检测服务器连接…", fg=COLORS["primary"])
        self._status_detail.configure(text="请稍候，检测期间仍可打开管理后台。")
        self._probe_thread = threading.Thread(target=_probe_worker, args=(self.base_url, self._outbox), daemon=True, name="desktop-remote-probe")
        try:
            self._probe_thread.start()
        except RuntimeError:
            self._outbox.put(("probe", RemoteServerStatus(False, "连接检测未完成", "请稍后点击“检测连接”重试。")))

    def open_site(self) -> None:
        if self._closed or self._browser_busy:
            return
        self._browser_busy = True
        self._open_button.configure(state=tk.DISABLED, text="正在打开…")
        self._browser_detail.configure(text="正在打开默认浏览器…", fg=COLORS["text_secondary"])
        self._browser_thread = threading.Thread(target=_browser_worker, args=(self.base_url, self._outbox), daemon=True, name="desktop-open-site")
        try:
            self._browser_thread.start()
        except RuntimeError:
            self._outbox.put(("browser", False))

    def _poll_results(self) -> None:
        self._poll_id = None
        if self._closed:
            return
        try:
            while True:
                try:
                    action, result = self._outbox.get_nowait()
                except queue.Empty:
                    break
                if action == "probe":
                    self._probe_busy = False
                    self._probe_button.configure(state=tk.NORMAL, text="检测连接")
                    color = ("#b45309" if result.degraded else COLORS["success"]) if result.connected else COLORS["error"]
                    self._status_label.configure(text=result.message, fg=color)
                    self._status_detail.configure(text=result.detail)
                elif action == "browser":
                    self._browser_busy = False
                    self._open_button.configure(state=tk.NORMAL, text="打开管理后台")
                    self._browser_detail.configure(
                        text="已请求打开浏览器，请在网页中登录管理后台。" if result else "未能自动打开浏览器，请复制上方服务器地址到浏览器访问。",
                        fg=COLORS["text_secondary"] if result else COLORS["error"],
                    )
            self._poll_id = self.root.after(self.POLL_MILLISECONDS, self._poll_results)
        except tk.TclError:
            self._closed = True

    def _cancel_poll(self) -> None:
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._closed = True
            self._cancel_poll()

    def close(self) -> None:
        """只关闭桌面窗口；后台工作线程不会访问 Tk，也不停止远程服务。"""
        if self._closed:
            return
        self._closed = True
        self._cancel_poll()
        self.root.destroy()

    def run(self) -> None:
        if not self._closed:
            self.root.mainloop()
