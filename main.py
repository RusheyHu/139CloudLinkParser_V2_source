import sys, os, re, json, time, base64, hashlib, random, string, urllib.parse, traceback
from pathlib import Path

from datetime import datetime, timezone, timedelta
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QMessageBox, QGroupBox, QFormLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QComboBox)
import requests
from playwright.sync_api import sync_playwright

APP_NAME = "中国移动云盘直链助手 V2"
PERSONAL_API = "https://personal-kd-njs.yun.139.com"
BASE_URL = "https://share-kd-njs.yun.139.com/yun-share/richlifeApp/devapp/IOutLink"
PROFILE_DIR = Path.home() / ".139_link_parser" / "browser_profile"
STATE_DIR = Path.home() / ".139_link_parser"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def parse_share(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("请先粘贴中国移动云盘分享链接")
    # Common PC and mobile share routes. Also accept a bare share ID optionally followed by @passcode.
    pwd = ""
    m_pwd = re.search(r"(?:提取码|访问码|密码)\s*[:：]?\s*([A-Za-z0-9]{4,8})", text, re.I)
    if m_pwd:
        pwd = m_pwd.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{8,40}(?:@[A-Za-z0-9]{4,8})?", text):
        bits = text.split("@", 1)
        return bits[0], bits[1] if len(bits) > 1 else pwd
    # hash route: #/w/i/<id> or #/w/group/i/<id>
    patterns = [r"#/w/(?:group/)?i/([A-Za-z0-9_-]+)", r"#/m/i\?([A-Za-z0-9_-]+)", r"[?&](?:shareId|linkID)=([A-Za-z0-9_-]+)"]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1), pwd
    raise ValueError("无法识别分享 ID。请粘贴完整的 yun.139.com 分享链接或分享 ID。")


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def get_account_from_cookies(cookies: dict) -> str:
    """
    The share-download API's `account` field expects the account identifier
    (normally the mobile number), not the encrypted cookie value itself.
    The official web client stores it as Base64 in ORCHES-I-ACCOUNT-ENCRYPT.
    """
    raw = (cookies or {}).get("ORCHES-I-ACCOUNT-ENCRYPT", "")
    if not raw:
        raise ValueError(
            "登录信息缺少 ORCHES-I-ACCOUNT-ENCRYPT。请重新扫码登录；"
            "登录完成后仍缺失时，请检查浏览器是否登录到了 yun.139.com。"
        )

    # Browser cookie values may be URL-escaped and Base64 padding may be omitted.
    raw = urllib.parse.unquote(str(raw)).strip().strip('"')
    raw += "=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8").strip()
    except Exception:
        raise ValueError(
            "无法解码登录 Cookie 中的账号标识。请退出程序后重新扫码登录，"
            "不要手动填写或分享 Cookie。"
        )

    # Do not expose the decoded account in logs or error messages.
    if not decoded or len(decoded) > 64 or any(ch.isspace() for ch in decoded):
        raise ValueError("登录 Cookie 解出的账号标识格式异常，请重新扫码登录。")
    return decoded


def make_headers(body, cookies):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    if body:
        compact = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = urllib.parse.quote(compact, safe="~()*!.'-")
        sorted_text = ''.join(sorted(encoded))
        first = md5(base64.b64encode(sorted_text.encode()).decode())
    else:
        first = md5(base64.b64encode(b"").decode())
    signature = md5(first + md5(stamp + ":" + nonce)).upper()
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://yun.139.com",
        "Referer": "https://yun.139.com/shareweb/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "mcloud-sign": f"{stamp},{nonce},{signature}",
        "mcloud-client": "10702",
        "mcloud-version": "7.13.3",
        "mcloud-channel": "1000101",
    }
    if cookies.get("skey"):
        h["mcloud-skey"] = cookies["skey"]
    if cookies.get("authorization"):
        h["Authorization"] = cookies["authorization"]
        h["x-m4c-token"] = cookies.get("auth_token", "")
        h["x-m4c-account"] = cookies.get("ORCHES-I-ACCOUNT-ENCRYPT", "")
    return h


class CloudClient:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}
        self.session = requests.Session()

    def post(self, endpoint, body):
        url = f"{BASE_URL}/{endpoint}"
        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        headers = make_headers(body, self.cookies)
        if cookie_str:
            headers["Cookie"] = cookie_str
        r = self.session.post(url, json=body, headers=headers, timeout=25)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"服务器返回非 JSON 内容（HTTP {r.status_code}）")
        code = str(data.get("resultCode", data.get("code", "")))
        desc = str(data.get("desc", data.get("message", "")))
        if code and code not in ("0", "000000", "200", "success", "200000000"):
            if code == "200000401" or "鉴权失败" in desc:
                raise PermissionError(f"账号鉴权失败（{code}）：{desc or '请重新扫码登录'}")
            if "提取码" in desc or "密码" in desc:
                raise ValueError(f"分享提取码错误或缺失：{desc}")
            raise RuntimeError(f"移动云盘接口返回错误 {code}：{desc or '未提供错误说明'}")
        return data

    def list_root(self, link_id, passwd, pca_id="root"):
        body = {"getOutLinkInfoReq": {
            "linkID": link_id, "passwd": passwd, "pCaID": pca_id,
            "bNum": 1, "eNum": 200, "srtDr": 1, "caSrt": "", "coSrt": ""
        }}
        return self.post("getOutLinkInfoV6", body)

    def get_direct(self, link_id, fpath):
        account = get_account_from_cookies(self.cookies)
        body = {"dlFromOutLinkReqV3": {
            "account": account, "linkID": link_id,
            "coIDLst": {"item": [fpath]}
        }}
        return self.post("dlFromOutLinkV3", body)

    def personal_post(self, endpoint, body):
        """Call the new personal-cloud API using the existing local login session."""
        if not self.cookies.get("authorization"):
            raise PermissionError("登录态中没有 authorization，请重新扫码登录。")
        # The service expects China-local time in the request signature.
        china_tz = timezone(timedelta(hours=8))
        stamp = datetime.now(china_tz).strftime("%Y-%m-%d %H:%M:%S")
        nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        compact = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = urllib.parse.quote(compact, safe="~()*!.'-")
        sorted_text = ''.join(sorted(encoded))
        first = md5(base64.b64encode(sorted_text.encode()).decode())
        signature = md5(first + md5(stamp + ":" + nonce)).upper()
        headers = {
            "Authorization": self.cookies["authorization"],
            "Caller": "web",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
            "CMS-DEVICE": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Sign": f"{stamp},{nonce},{signature}",
            "Mcloud-Version": "7.14.2",
            "X-DeviceInfo": "||9|7.17.0|edge||||windows 10||zh-CN|||",
            "X-Huawei-ChannelSrc": "10000034",
            "X-Inner-Ntwk": "2",
            "X-M4C-Caller": "PC",
            "X-M4C-Src": "10002",
            "X-SvcType": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.17.0|edge||||windows 10||zh-CN|||||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
            "X-Yun-Url-Type": "3",
        }
        r = self.session.post(PERSONAL_API + endpoint, json=body, headers=headers, timeout=30)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"个人云盘接口返回非 JSON 内容（HTTP {r.status_code}）")
        if not data.get("success", False):
            code = str(data.get("code", "未知"))
            message = str(data.get("message", "请求失败"))
            if code in ("401", "403", "200000401") or "鉴权" in message or "登录" in message:
                raise PermissionError(f"个人云盘鉴权失败（{code}）：{message}")
            raise RuntimeError(f"个人云盘接口错误（{code}）：{message}")
        return data

    def list_personal_files(self, parent_id="/"):
        items_all = []
        cursor = None
        while True:
            body = {
                "pageInfo": {"pageSize": 100, "pageCursor": cursor},
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "parentFileId": parent_id,
                "imageThumbnailStyleList": ["Small", "Large"],
            }
            result = self.personal_post("/hcy/file/list", body)
            data = result.get("data") or {}
            items_all.extend(data.get("items") or [])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        return items_all

    def get_personal_direct(self, file_id):
        result = self.personal_post("/hcy/file/getDownloadUrl", {"fileId": file_id})
        data = result.get("data") or {}
        url = data.get("cdnUrl") or data.get("url")
        if not url:
            raise RuntimeError("接口请求成功，但响应中没有 cdnUrl 或 url 字段。")
        return str(url)


class LoginWorker(QThread):
    log = Signal(str)
    done = Signal(bool, str)

    def run(self):
        try:
            self.log.emit("正在打开中国移动云盘网页，请在浏览器中使用移动云盘 App 扫码登录……")
            with sync_playwright() as p:
                browser_type = p.chromium
                launch_args = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
                # Prefer installed Microsoft Edge; fall back to installed Google Chrome.
                try:
                    context = browser_type.launch_persistent_context(str(PROFILE_DIR), channel="msedge", **launch_args)
                except Exception:
                    context = browser_type.launch_persistent_context(str(PROFILE_DIR), channel="chrome", **launch_args)
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                page.goto("https://yun.139.com/shareweb/", wait_until="domcontentloaded", timeout=60000)
                self.log.emit("浏览器已打开。请完成扫码；程序正在等待登录态 Cookie……")
                deadline = time.time() + 240
                found = {}
                while time.time() < deadline:
                    try:
                        all_cookies = context.cookies(["https://yun.139.com", "https://139.com", "https://personal-kd-njs.yun.139.com"])
                        found = {c["name"]: c["value"] for c in all_cookies}
                        # Persist all first-party cookies in the Playwright profile; only required names are checked.
                        if found.get("authorization") and (found.get("auth_token") or found.get("skey")):
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                context.close()
                if not found.get("authorization"):
                    self.done.emit(False, "等待超时或未发现有效登录 Cookie。请确认已在网页中完成扫码登录后重试。")
                    return
                wanted = ["authorization", "auth_token", "token", "skey", "a_k", "ud_id", "ORCHES-I-ACCOUNT-ENCRYPT"]
                saved = {k: found[k] for k in wanted if found.get(k)}
                (STATE_DIR / "cookies.json").write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
                self.done.emit(True, "扫码登录态已保存到本机。不会上传到第三方服务器。")
        except Exception as e:
            self.done.emit(False, f"扫码登录失败：{e}")


class ParseWorker(QThread):
    log = Signal(str)
    success = Signal(str, str)
    failed = Signal(str)

    def __init__(self, link_text, passcode):
        super().__init__()
        self.link_text = link_text
        self.passcode = passcode

    def run(self):
        try:
            link_id, embedded_pwd = parse_share(self.link_text)
            pwd = self.passcode.strip() or embedded_pwd
            self.log.emit(f"已识别分享 ID：{link_id}\n正在读取分享文件信息……")
            cookie_file = STATE_DIR / "cookies.json"
            cookies = json.loads(cookie_file.read_text(encoding="utf-8")) if cookie_file.exists() else {}
            client = CloudClient(cookies)
            info = client.list_root(link_id, pwd)
            data = info.get("data", info)
            files = data.get("coLst") or data.get("fileList") or []
            folders = data.get("caLst") or []
            if not files:
                if folders:
                    self.failed.emit("分享目录中包含子文件夹。当前版本尚未递归列出子目录，请先打开分享页确认文件位置。")
                    return
                self.failed.emit("已请求分享信息，但接口没有返回文件列表。请检查分享是否有效、是否需要提取码。")
                return
            if len(files) > 1:
                # Present all direct URLs only when one file can be unambiguously selected.
                lines = []
                for i, f in enumerate(files, 1):
                    lines.append(f"{i}. {f.get('coName', '未命名文件')}（{f.get('coSize', '?')} 字节）")
                self.failed.emit("该分享包含多个文件。当前版本为避免复制错误，暂不自动选择。\n" + "\n".join(lines))
                return
            f = files[0]
            fpath = f.get("path")
            if not fpath:
                raise RuntimeError("分享信息中没有返回文件 path，无法申请下载直链。")
            self.log.emit(f"文件：{f.get('coName', '未命名文件')}\n正在申请下载直链……")
            result = client.get_direct(link_id, fpath)
            payload = result.get("data", result)
            direct = payload.get("redrUrl") or payload.get("downloadUrl") or payload.get("url")
            if not direct:
                raise RuntimeError("接口已响应，但没有返回 redrUrl。可能是账号鉴权、分享权限或接口版本发生变化。")
            self.success.emit(str(f.get("coName", "文件")), str(direct))
        except PermissionError as e:
            self.failed.emit(f"需要登录授权：{e}\n请先点击“扫码登录”，完成登录后重新解析。")
        except Exception as e:
            self.failed.emit(str(e))


class PersonalListWorker(QThread):
    done = Signal(bool, object, str)
    def __init__(self, parent_id):
        super().__init__(); self.parent_id = parent_id
    def run(self):
        try:
            cookies = json.loads((STATE_DIR / "cookies.json").read_text(encoding="utf-8"))
            items = CloudClient(cookies).list_personal_files(self.parent_id)
            self.done.emit(True, items, "读取完成")
        except Exception as e:
            self.done.emit(False, [], str(e))


class PersonalDirectWorker(QThread):
    done = Signal(bool, str, str)
    def __init__(self, file_id, filename):
        super().__init__(); self.file_id = file_id; self.filename = filename
    def run(self):
        try:
            cookies = json.loads((STATE_DIR / "cookies.json").read_text(encoding="utf-8"))
            url = CloudClient(cookies).get_personal_direct(self.file_id)
            self.done.emit(True, self.filename, url)
        except Exception as e:
            self.done.emit(False, self.filename, str(e))


class ShareListWorker(QThread):
    done = Signal(bool, object, object, str)
    def __init__(self, link_text, passcode, pca_id="root"):
        super().__init__(); self.link_text = link_text; self.passcode = passcode; self.pca_id = pca_id
    def run(self):
        try:
            link_id, embedded_pwd = parse_share(self.link_text)
            pwd = self.passcode.strip() or embedded_pwd
            cookies = json.loads((STATE_DIR / "cookies.json").read_text(encoding="utf-8")) if (STATE_DIR / "cookies.json").exists() else {}
            client = CloudClient(cookies)
            response = client.list_root(link_id, pwd, self.pca_id)
            data = response.get("data", response)
            files = data.get("coLst") or data.get("fileList") or []
            folders = data.get("caLst") or []
            self.done.emit(True, files, folders, link_id)
        except Exception as e:
            self.done.emit(False, [], [], str(e))


class ShareDirectWorker(QThread):
    done = Signal(bool, str, str)
    def __init__(self, link_id, fpath, filename):
        super().__init__(); self.link_id = link_id; self.fpath = fpath; self.filename = filename
    def run(self):
        try:
            cookies = json.loads((STATE_DIR / "cookies.json").read_text(encoding="utf-8"))
            url = CloudClient(cookies).get_direct(self.link_id, self.fpath)
            payload = url.get("data", url)
            direct = payload.get("redrUrl") or payload.get("downloadUrl") or payload.get("url")
            if not direct: raise RuntimeError("分享接口没有返回下载直链。")
            self.done.emit(True, self.filename, str(direct))
        except Exception as e:
            self.done.emit(False, self.filename, str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1120, 720)
        self.setMinimumSize(900, 600)
        self.current_parent_id = "/"
        self.share_link_id = ""
        self.share_current_pca = "root"
        self.share_folder_stack = [("分享根目录", "root")]
        self.share_files = []
        self.share_folders = []
        self.share_list_worker = None
        self.share_direct_worker = None
        self.current_path = [("我的云盘", "/")]
        self.personal_items = []
        self.direct_url = ""
        self.list_worker = None
        self.direct_worker = None
        self.login_worker = None
        self.parse_worker = None

        root = QWidget(); self.setCentralWidget(root)
        layout = QVBoxLayout(root); layout.setSpacing(10)
        title_row = QHBoxLayout()
        title = QLabel("中国移动云盘直链助手")
        title.setStyleSheet("font-size:23px;font-weight:700;color:#1f2937")
        self.login_status = QLabel("登录状态：未检测")
        self.login_status.setStyleSheet("color:#64748b")
        self.login_btn = QPushButton("扫码登录")
        self.login_btn.clicked.connect(self.login)
        title_row.addWidget(title); title_row.addStretch(1); title_row.addWidget(self.login_status); title_row.addWidget(self.login_btn)
        layout.addLayout(title_row)
        subtitle = QLabel("读取个人云盘或分享文件；解析后只复制下载直链，不会自动下载文件。")
        subtitle.setStyleSheet("color:#64748b")
        layout.addWidget(subtitle)

        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        # Personal cloud tab
        personal = QWidget(); pv = QVBoxLayout(personal)
        toolbar = QHBoxLayout()
        self.path_label = QLabel("我的云盘  /")
        self.search_box = QLineEdit(); self.search_box.setPlaceholderText("搜索当前目录文件名称…")
        self.search_box.textChanged.connect(self.filter_personal_table)
        self.refresh_btn = QPushButton("刷新列表"); self.refresh_btn.clicked.connect(self.load_personal_files)
        self.up_btn = QPushButton("返回上级"); self.up_btn.clicked.connect(self.go_parent)
        toolbar.addWidget(self.path_label, 2); toolbar.addWidget(self.search_box, 3); toolbar.addWidget(self.up_btn); toolbar.addWidget(self.refresh_btn)
        pv.addLayout(toolbar)
        self.personal_table = QTableWidget(0, 5)
        self.personal_table.setHorizontalHeaderLabels(["文件名称", "类型", "大小", "修改时间", "操作"])
        self.personal_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.personal_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.personal_table.verticalHeader().setVisible(False)
        self.personal_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1,2,3,4): self.personal_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.personal_table.cellDoubleClicked.connect(self.personal_row_double_clicked)
        pv.addWidget(self.personal_table, 1)
        self.personal_status = QLabel("请先扫码登录，然后点击“刷新列表”。")
        self.personal_status.setStyleSheet("color:#64748b")
        pv.addWidget(self.personal_status)
        self.tabs.addTab(personal, "我的云盘")

        # Share tab: read the share directory and expose per-file copy actions.
        share = QWidget(); sv = QVBoxLayout(share)
        group = QGroupBox("分享文件")
        form = QFormLayout(group)
        self.link = QTextEdit(); self.link.setPlaceholderText("粘贴 https://yun.139.com/shareweb/#/w/i/... 分享链接")
        self.link.setMaximumHeight(70)
        self.pwd = QLineEdit(); self.pwd.setPlaceholderText("没有提取码可留空")
        form.addRow("分享链接：", self.link); form.addRow("提取码：", self.pwd)
        sv.addWidget(group)
        share_toolbar = QHBoxLayout()
        self.share_load_btn = QPushButton("读取分享文件列表")
        self.share_up_btn = QPushButton("返回分享根目录"); self.share_up_btn.setEnabled(False)
        self.share_load_btn.clicked.connect(self.load_share_files); self.share_up_btn.clicked.connect(self.share_go_root)
        share_toolbar.addWidget(self.share_load_btn); share_toolbar.addWidget(self.share_up_btn); share_toolbar.addStretch(1)
        sv.addLayout(share_toolbar)
        self.share_table = QTableWidget(0, 4)
        self.share_table.setHorizontalHeaderLabels(["文件名称", "类型", "大小", "操作"])
        self.share_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.share_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.share_table.verticalHeader().setVisible(False)
        self.share_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1,2,3): self.share_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.share_table.cellDoubleClicked.connect(self.share_row_double_clicked)
        sv.addWidget(self.share_table, 1)
        self.share_status = QLabel("粘贴分享链接后，点击“读取分享文件列表”。")
        self.share_status.setStyleSheet("color:#64748b")
        sv.addWidget(self.share_status)
        self.output = QTextEdit(); self.output.setReadOnly(True); self.output.setMaximumHeight(90)
        self.output.setPlaceholderText("分享解析状态显示在这里……")
        sv.addWidget(self.output)
        self.tabs.addTab(share, "分享文件")

        foot = QLabel("安全提示：登录凭证仅保存在当前 Windows 用户目录；不要分享 cookies.json。直链可能过期，请获取后及时使用。")
        foot.setWordWrap(True); foot.setStyleSheet("color:#94a3b8;font-size:11px")
        layout.addWidget(foot)
        self.load_example()
        if (STATE_DIR / "cookies.json").exists():
            self.login_status.setText("登录状态：已保存登录信息")

    def load_example(self):
        # Do not preload a sample share URL; leave the user's input clean.
        self.link.clear()
        self.output.setPlainText("请粘贴分享链接后解析。")

    def login(self):
        self.login_btn.setEnabled(False)
        self.login_status.setText("登录状态：等待扫码…")
        self.output.append("\n开始扫码登录……")
        self.login_worker = LoginWorker()
        self.login_worker.log.connect(self.output.append)
        self.login_worker.done.connect(self.login_done)
        self.login_worker.start()

    def login_done(self, ok, message):
        self.login_btn.setEnabled(True)
        self.login_status.setText("登录状态：已登录" if ok else "登录状态：未登录")
        self.output.append(("✓ " if ok else "✗ ") + message)
        if ok:
            self.load_personal_files()
        else:
            QMessageBox.warning(self, "扫码登录", message)

    def load_personal_files(self):
        cookie_file = STATE_DIR / "cookies.json"
        if not cookie_file.exists():
            QMessageBox.information(self, "需要登录", "请先点击右上角“扫码登录”，完成登录后再读取云盘文件。")
            return
        self.refresh_btn.setEnabled(False)
        self.personal_status.setText("正在读取文件列表…")
        self.list_worker = PersonalListWorker(self.current_parent_id)
        self.list_worker.done.connect(self.personal_list_done)
        self.list_worker.start()

    def personal_list_done(self, ok, items, message):
        self.refresh_btn.setEnabled(True)
        if not ok:
            self.personal_status.setText("读取失败：" + message)
            if "鉴权" in message or "登录" in message:
                self.login_status.setText("登录状态：需要重新登录")
            QMessageBox.warning(self, "读取云盘失败", message)
            return
        self.personal_items = items
        self.render_personal_table(items)
        self.personal_status.setText(f"读取完成：{len(items)} 个项目")

    @staticmethod
    def format_size(size):
        try:
            size = int(size or 0)
        except Exception:
            return "—"
        if size <= 0: return "—"
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]: return f"{value:.1f} {unit}"
            value /= 1024

    def render_personal_table(self, items):
        self.personal_table.setRowCount(0)
        for item in items:
            row = self.personal_table.rowCount(); self.personal_table.insertRow(row)
            is_folder = item.get("type") == "folder"
            name = str(item.get("name") or "未命名")
            name_item = QTableWidgetItem(("📁  " if is_folder else "📄  ") + name)
            name_item.setData(Qt.ItemDataRole.UserRole, item)
            self.personal_table.setItem(row, 0, name_item)
            self.personal_table.setItem(row, 1, QTableWidgetItem("文件夹" if is_folder else (item.get("fileExtension") or "文件")))
            self.personal_table.setItem(row, 2, QTableWidgetItem("—" if is_folder else self.format_size(item.get("size"))))
            self.personal_table.setItem(row, 3, QTableWidgetItem(str(item.get("updatedAt") or "—").replace("T", " ")[:19]))
            if is_folder:
                open_btn = QPushButton("打开文件夹")
                open_btn.clicked.connect(lambda checked=False, data=item: self.open_folder(data))
                self.personal_table.setCellWidget(row, 4, open_btn)
            else:
                copy_btn = QPushButton("解析并复制")
                copy_btn.clicked.connect(lambda checked=False, data=item: self.get_personal_direct(data))
                self.personal_table.setCellWidget(row, 4, copy_btn)
        self.filter_personal_table(self.search_box.text())

    def filter_personal_table(self, text):
        needle = (text or "").strip().lower()
        for row in range(self.personal_table.rowCount()):
            item = self.personal_table.item(row, 0)
            self.personal_table.setRowHidden(row, bool(needle) and needle not in item.text().lower())

    def open_folder(self, item):
        self.current_parent_id = str(item.get("fileId"))
        self.current_path.append((str(item.get("name") or "文件夹"), self.current_parent_id))
        self.path_label.setText(" / ".join(x[0] for x in self.current_path))
        self.load_personal_files()

    def go_parent(self):
        if len(self.current_path) <= 1: return
        self.current_path.pop()
        self.current_parent_id = self.current_path[-1][1]
        self.path_label.setText(" / ".join(x[0] for x in self.current_path))
        self.load_personal_files()

    def personal_row_double_clicked(self, row, column):
        item = self.personal_table.item(row, 0)
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        if data and data.get("type") == "folder": self.open_folder(data)

    def get_personal_direct(self, item):
        self.personal_status.setText("正在申请直链：" + str(item.get("name") or "文件"))
        self.direct_worker = PersonalDirectWorker(str(item.get("fileId")), str(item.get("name") or "文件"))
        self.direct_worker.done.connect(self.personal_direct_done)
        self.direct_worker.start()

    def personal_direct_done(self, ok, filename, result):
        if not ok:
            self.personal_status.setText("解析失败：" + result)
            QMessageBox.warning(self, "直链解析失败", result)
            return
        QApplication.clipboard().setText(result)
        self.personal_status.setText(f"已复制：{filename}")
        QMessageBox.information(self, "复制成功", f"文件：{filename}\n下载直链已复制到剪贴板。")

    def load_share_files(self):
        if not self.link.toPlainText().strip():
            QMessageBox.information(self, "需要分享链接", "请先粘贴中国移动云盘分享链接。")
            return
        self.share_load_btn.setEnabled(False)
        self.share_status.setText("正在读取分享目录…")
        self.share_list_worker = ShareListWorker(self.link.toPlainText(), self.pwd.text(), self.share_current_pca)
        self.share_list_worker.done.connect(self.share_list_done)
        self.share_list_worker.start()

    def share_list_done(self, ok, files, folders, info):
        self.share_load_btn.setEnabled(True)
        if not ok:
            self.share_status.setText("读取失败：" + str(info))
            QMessageBox.warning(self, "读取分享列表失败", str(info))
            return
        self.share_link_id = str(info)
        self.share_files = files or []; self.share_folders = folders or []
        self.share_table.setRowCount(0)
        for folder in self.share_folders:
            row = self.share_table.rowCount(); self.share_table.insertRow(row)
            name = str(folder.get("caName") or folder.get("name") or "未命名文件夹")
            cell = QTableWidgetItem("📁  " + name); cell.setData(Qt.ItemDataRole.UserRole, folder)
            self.share_table.setItem(row, 0, cell)
            self.share_table.setItem(row, 1, QTableWidgetItem("文件夹"))
            self.share_table.setItem(row, 2, QTableWidgetItem("—"))
            btn = QPushButton("打开文件夹")
            btn.setEnabled(bool(folder.get("caID") or folder.get("pCaID") or folder.get("id")))
            btn.clicked.connect(lambda checked=False, data=folder: self.open_share_folder(data))
            self.share_table.setCellWidget(row, 3, btn)
        for file in self.share_files:
            row = self.share_table.rowCount(); self.share_table.insertRow(row)
            name = str(file.get("coName") or file.get("name") or "未命名文件")
            cell = QTableWidgetItem("📄  " + name); cell.setData(Qt.ItemDataRole.UserRole, file)
            self.share_table.setItem(row, 0, cell)
            self.share_table.setItem(row, 1, str(file.get("coSuffix") or file.get("fileExtension") or "文件"))
            self.share_table.setItem(row, 2, QTableWidgetItem(self.format_size(file.get("coSize") or file.get("size"))))
            btn = QPushButton("解析并复制")
            btn.setEnabled(bool(file.get("path")))
            btn.clicked.connect(lambda checked=False, data=file: self.get_share_direct(data))
            self.share_table.setCellWidget(row, 3, btn)
        self.share_up_btn.setEnabled(len(self.share_folder_stack) > 1)
        self.share_status.setText(f"分享目录读取完成：{len(self.share_folders)} 个文件夹，{len(self.share_files)} 个文件")

    def open_share_folder(self, folder):
        folder_id = folder.get("caID") or folder.get("pCaID") or folder.get("id")
        if not folder_id: return
        name = str(folder.get("caName") or folder.get("name") or "文件夹")
        self.share_current_pca = str(folder_id)
        self.share_folder_stack.append((name, self.share_current_pca))
        self.load_share_files()

    def share_go_root(self):
        self.share_current_pca = "root"; self.share_folder_stack = [("分享根目录", "root")]
        self.load_share_files()

    def share_row_double_clicked(self, row, column):
        cell = self.share_table.item(row, 0)
        data = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        if data and (data.get("caID") or data.get("pCaID")):
            self.open_share_folder(data)

    def get_share_direct(self, file):
        self.share_status.setText("正在申请直链：" + str(file.get("coName") or "文件"))
        self.share_direct_worker = ShareDirectWorker(self.share_link_id, str(file.get("path")), str(file.get("coName") or "文件"))
        self.share_direct_worker.done.connect(self.share_direct_done)
        self.share_direct_worker.start()

    def share_direct_done(self, ok, filename, result):
        if not ok:
            self.share_status.setText("解析失败：" + result)
            QMessageBox.warning(self, "分享直链解析失败", result)
            return
        QApplication.clipboard().setText(result)
        self.share_status.setText("已复制：" + filename)
        QMessageBox.information(self, "复制成功", f"文件：{filename}\n下载直链已复制到剪贴板。")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
