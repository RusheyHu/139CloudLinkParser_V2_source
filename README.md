# 中国移动云盘直链助手 V2（开发版）

## 本版改造
- 保留原来的 Playwright 浏览器扫码登录方式。
- 新增“我的云盘”页面：调用 `https://personal-kd-njs.yun.139.com/hcy/file/list` 读取个人云盘目录。
- 支持根目录、进入文件夹、返回上级、刷新和当前目录名称搜索。
- 个人云盘文件行右侧增加“解析并复制”，调用 `/hcy/file/getDownloadUrl`，从 `data.cdnUrl` 或 `data.url` 读取直链。
- 新增“分享文件”页面：读取分享目录，文件行右侧提供“解析并复制”；分享直链仍使用原分享接口。
- 不自动下载文件，只把下载 URL 写入 Windows 剪贴板。
- 登录 Cookie 保存在当前 Windows 用户目录 `~/.139_link_parser/cookies.json`，请勿分享该文件。

## 重要说明
这是基于你提供的个人云盘文件列表请求/响应样本，以及公开的接口实现资料制作的开发版。个人云盘列表接口和直链接口的请求结构已写入程序，但**尚未使用你的真实登录态在你的账号上完成端到端验证**。分享目录的文件夹字段在不同分享类型下可能不同，若文件夹无法进入，需要再提供该文件夹的脱敏响应样本。

## Windows 本地打包
1. Windows 10/11 安装 Python 3.11。
2. 安装 Microsoft Edge 或 Google Chrome。
3. 解压项目，在项目目录双击 `build_windows.bat`。
4. 输出位于 `dist\139CloudLinkParserV2\139CloudLinkParserV2.exe`。
5. 发布时保留整个 `dist\139CloudLinkParserV2` 文件夹。

## GitHub Actions 打包
将项目全部文件（包括 `.github` 目录）上传到 GitHub 仓库。进入 Actions，运行 `Build Windows EXE`，完成后下载 Artifacts 中的 `139CloudLinkParserV2-Windows-x64`。

## 使用
1. 启动程序，点击右上角“扫码登录”，按原方式完成扫码。
2. “我的云盘”页点击“刷新列表”，双击文件夹进入目录。
3. 在文件行右侧点击“解析并复制”，程序会把直链复制到剪贴板。
4. “分享文件”页粘贴分享链接和提取码，点击“读取分享文件列表”，再对目标文件点击“解析并复制”。

## 安全
- 不要把 `cookies.json`、完整下载直链或带签名参数的 URL 发给他人。
- 直链可能有时效；获取后及时使用。
- 如果浏览器对下载域名显示安全警告，不要通过关闭安全防护或修改跳转来绕过警告。
