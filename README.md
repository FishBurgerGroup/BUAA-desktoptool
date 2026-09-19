# UBAA Desktop

一个 Windows 优先的桌面摘要工具：显示当前课程、下一课程、倒计时和未完成作业。

## 运行

需要 Python 3.11+ 及依赖：

```powershell
python -m pip install -r requirements.txt
python app.py
```

也可以直接双击 `start.bat`；它通过 `pythonw.exe` 启动，不保留命令行窗口。

课程与作业卡片使用 Pillow 进行 4 倍超采样圆角渲染。透明模式顶部使用 Windows 逐像素 Alpha layered overlay：文字尺寸按原生 Tk 字体实际宽高匹配，但抗锯齿像素直接与桌面合成，不经过单色透明键。

首次启动没有登录令牌时使用演示数据。点击“设置 / 登录”，填写 UBAA relay API 地址和校园账号即可连接。登录成功后，账号密码使用 Windows DPAPI 加密并绑定到当前 Windows 用户，密文保存在 `%APPDATA%\\UBAA Desktop\\credentials.bin`；登录窗口会自动填充，点击“清除登录信息”可删除。窗口拖动后的位置保存在独立的 `ui-state.json`，重启后自动恢复。

设置页提供“透明背景（隐藏外层底板）”选项。该选项会移除小工具最外层浅色底板、同步状态和操作按钮，课程卡片与待完成作业圆角卡片仍保留；顶部使用蓝色斜体 BUAA 字标、蓝色日期以及黑色年份和星期。透明模式下通过右键菜单刷新、设置或退出。外观选项与窗口位置一起持久化到 `ui-state.json`。

## 验证

```powershell
python -m unittest -v
```

服务器中转模式调用 UBAA 的只读摘要接口：`/api/v1/schedule/today`、`/api/v1/spoc/assignments` 和 `/api/v1/judge/assignments`。直连/WebVPN 模式复用 UBAA 的 CAS、UC、本科教务、SPOC token 分页接口以及希冀课程/作业页面，并只显示未提交且未截止的作业。本地 Cookie 与最近成功摘要保存在用户应用数据目录，不写入仓库。

本地模式同步异常时，脱敏日志写入 `%APPDATA%\\UBAA Desktop\\debug.log`，只包含阶段、主机、路径、HTTP 状态和响应大小，不包含密码、令牌、Cookie 或响应正文。DPAPI 可防止直接读取磁盘文件获得密码，但无法防御已控制当前 Windows 登录会话的恶意程序。
## 警告
本软件经过AI辅助生成，本作者精力有限没有进行完整的安全检查，本软件非商用性质，使用本软件带来的任何问题作者不承担任何责任（包括因为课表同步问题导致你错过了某些作业的DDL）请自行斟酌后再下载使用。
## 特别鸣谢
本软件开发过程中大量参考UBAA项目中的代码,在此感谢UBAA全体开发人员。
