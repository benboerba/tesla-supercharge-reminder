# Tesla 超充满位提醒 · AI 助手搭建包

下载仓库 ZIP 或克隆仓库后，把文件夹交给自己的 AI 助手，再发送 `给AI助手的开场指令.txt`。支持读取文件的 AI 即可使用；不要求安装专属插件。支持 skill 的工具可把整个文件夹安装到它自己的 skills 目录，入口是 SKILL.md。

包含可运行的脱敏源码、中文搭建 skill、产品思路、部署模板和验收清单。每轮查询结束约 60 秒后再查。开启前按用户选择 Bark 提醒方式：每分钟报空位（首次默认）、快满时提醒、只在该挪车时提醒。拔枪结束，开始匹配时需要位置，识别后不再主动请求位置。代码并不能检测是否已产生占用费。

你仍需自己的 Tesla 开发者应用、HTTPS 域名、常驻服务器和 Bark。不是把原作者的网址改个名字就能上线；授权、域名和回调必须属于部署者。

项目包含完整源码和 AI 助手搭建说明。未包含真实密钥、邀请码、令牌、车辆或数据库。`assets/starter/shared-charge/THIRD_PARTY_LICENSES.txt` 保留第三方许可。第一版适合自用或少量受邀测试，其他地区和多车型须自行验证。

许可：本包原创代码及文档按 MIT 授权，第三方代码按各自许可。测试模式、收费规则、站点数据和通知效果不构成免收费保证。

从第一次部署到手机收到空位通知，可对照[中国大陆实车示例 · 上海绚荟城](references/china-example.md)。

## 快速开始

```sh
git clone https://github.com/benboerba/tesla-supercharge-reminder.git
cd tesla-supercharge-reminder
```

- 从开发者账号、回调地址到上线验收的制作步骤：[AI 搭建 Skill](SKILL.md)。把整个仓库交给助手，复制[开场指令](给AI助手的开场指令.txt)即可开始。
- 人工搭建：[部署说明](references/deploy.md)
- 原理与限制：[产品与实现](references/design.md)
- 验收：[验证与排错](references/verification.md)
- 源码：[assets/starter](assets/starter)

每轮查询结束约 60 秒后再查。开启前按用户选择 Bark 提醒方式：每分钟报空位（首次默认）、快满时提醒、只在该挪车时提醒。提醒依赖 Tesla 返回数据的新鲜度；它不识别实际收费状态。首次匹配可能需要手动选站，拔枪结束需等待下一次成功查询。
