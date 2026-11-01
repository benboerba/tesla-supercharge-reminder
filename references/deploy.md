# 从零部署

## 需要部署者准备

- 自己的 Tesla 开发者应用与允许的车辆权限、自己的 HTTPS 域名、能常驻运行 Python 的 Linux 服务器。
- iPhone 和 Bark；每位车主单独授权、独立邀请码和设备密钥。
- 推荐 Python 3.10+、Node.js 20+、npm、Nginx。后端只用 Python 标准库；fcntl 不支持原生 Windows，可用 Linux/WSL。
- 中国区模板不可直接当成其他地区模板。Tesla 账户地区决定 API/授权域名，不能仅凭服务器所在地选区。

## 官方入口（搭建当天复核）

- https://developer.tesla.cn/docs/fleet-api/authentication/third-party-tokens
- https://developer.tesla.cn/docs/fleet-api/authentication/overview
- https://developer.tesla.cn/docs/fleet-api/getting-started/regions-countries
- https://developer.tesla.cn/docs/fleet-api/endpoints/vehicle-endpoints
- https://developer.tesla.com/docs/fleet-api/billing-and-limits
- Bark 项目与接口说明：https://github.com/Finb/Bark

中国区样板使用 API `https://fleet-api.prd.cn.vn.cloud.tesla.cn`、授权 `https://auth.tesla.cn/oauth2/v3/authorize`、换令牌 `https://auth.tesla.cn/oauth2/v3/token`。
申请应用、登记回调/来源域名、开发者域名公钥与 partner 注册等要求，以当前官方入门流程和实际应用状态为准；已有应用先检查，不重复注册。若要求 public-key.pem，只公开公钥，私钥留在受限目录。此包不替用户创建开发者账户或绕过审核。

请求权限为 openid offline_access vehicle_device_data vehicle_location；以控制台当日可选项和返回授权为准。没有位置权限会被本程序拒绝。无需请求远程控车权限来完成本工具。

## 本地构建与测试（在 starter 根目录）

```sh
cd shared-charge
npm ci
npm run build
python3 -B -m unittest -q test_shared
cd ..
python3 -B -m unittest -q test_tesla_watch test_auto_monitor
```

测试使用假凭据、mock Tesla/Bark，不调用真实车辆。test-data 是生成的模拟数据库，不可打包发布。

本地仅预览页面：设置 CHARGE_ORIGIN=http://127.0.0.1:5330，运行 python3 -B shared-charge/server.py。不要把本地 HTTP 地址当可用的真实 OAuth 回调。

## 配置和部署

1. 将 starter 内容复制到 `/opt/tesla-charge`，保留 shared-charge 相对路径。创建非 root 服务用户 tesla-charge；准备根目录 `.private/` 和 `shared-charge/.private/`，目录 700、私有配置 600，运行用户可读写需要的目录。
2. 把 config.example.json 作为格式参考，在服务器私有目录写入 `.private/config.json`。填写自己的 client_id/client_secret，回调必须是 `https://你的域名/charge/auth/callback`，与 Tesla 控制台完全一致；密钥不要通过聊天、Git、命令行参数或截图公开。
3. 将 deploy/service.env.example 按实际值保存到 `/etc/tesla-charge.env`，设为 600。CHARGE_ORIGIN 只能是来源（例如 https://your-domain.example），不要尾斜杠或 /charge/app/ 路径。默认 full_only；现场才改 diagnostic。
4. 将 deploy/tesla-charge.service 安装为 systemd 服务。若更改路径，同步修改工作目录、ExecStart、配置路径和 ReadWritePaths；不要 root 启动应用。服务需要访问外部 Tesla 和 api.day.app。
5. 先完成 DNS 与有效 HTTPS 证书。在自己的 HTTPS Nginx server 块 include deploy/nginx-locations.conf 的两个 location。新部署直接把应用和回调都送到 5330，不需要原项目双服务分流。保留其他站点配置，备份后 nginx -t 成功才 reload。
6. 使用 `systemctl enable --now tesla-charge` 启动。健康检查 `https://你的域名/charge/app/api/health` 应返回 200；匿名 me 应 401。确保 5330 仅监听 loopback，不对公网开放。
7. 用运行用户、相同 CHARGE_ORIGIN 环境生成自己的邀请码：

```sh
sudo -u tesla-charge env CHARGE_ORIGIN=https://your-domain.example python3 -B /opt/tesla-charge/shared-charge/admin.py '车友一' --output /opt/tesla-charge/shared-charge/.private/invite-1.txt
```

将生成文件仅交给对应车主。每个车主重新生成独立邀请码，不共用一个入口。
8. 车主在 `/charge/app/` 登录→Tesla 官方授权→读取并选车→填 Bark key→发测试并确认手机收到→现场开始。OAuth 操作需在同一浏览器会话完成。

回滚时停止服务、恢复备份代码和配置、验证后重启；保留数据与备份，不删除用户资料。不要从旧备份覆盖刷新过的 token，否则可能导致授权失效。
