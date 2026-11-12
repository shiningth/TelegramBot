# 获取 Telegram API 与登录

## 1. 获取 api_id / api_hash

本项目使用 Telethon（MTProto），即使登录机器人，也需要自己的 `api_id` 和 `api_hash`。

1. 先用 Telegram 官方客户端注册并登录自己的账号。
2. 打开 [my.telegram.org](https://my.telegram.org)，输入带国家区号的手机号，例如 `+86…`，按页面提示输入登录验证码。
3. 打开 **API development tools**；已有应用时会直接显示应用信息。
4. 没有应用时，填写 **App title**（应用名称）和 **Short name**（简短名称），按表单要求选择平台并创建应用。
5. 保存页面中的 **App api_id** 和 **App api_hash**，分别填入后台 Telegram 设置或配置文件的 `telegram.api_id`、`telegram.api_hash`。

这是 Telegram 提供的申请流程：[官方说明](https://core.telegram.org/api/obtaining_api_id)。每个手机号目前只能关联一个 API ID；请使用自己的凭据，不要使用他人公开的示例值。

## 2. 获取机器人 Token

1. 打开官方 [@BotFather](https://t.me/BotFather)，发送 `/newbot`。
2. 按提示设置显示名称和机器人用户名，保存返回的 Token。
3. 将 Token 填入 `telegram.bot_token`。**Bot Token 和 api_hash 是不同的凭据，不能互相替代。**
4. 将机器人加入需要监听的群，填写群 ID，最后启用 Telegram。

使用中文 `/导入` 时，需要机器人能收到普通群消息：可设为群管理员，或在 BotFather 发送 `/setprivacy`、选择机器人、点击 **Disable**，然后将机器人移出群再加入。导入授权另由 `magnet_import.sender_ids` 控制。

## 3. 登录个人账号用于自动签到

机器人 Token 只能登录机器人。给自己的账号签到时，需额外登录个人会话；这不会更换群机器人的会话。

在 Portainer 的 TelegramBot 容器 **Console** 中选择 `/bin/bash` 并连接。先配置好 api_id / api_hash，然后执行：

```bash
python -c 'import json; from telethon.sync import TelegramClient; c=json.load(open("/data/config/config.json"))["telegram"]; client=TelegramClient("/app/sessions/checkin_user",c["api_id"],c["api_hash"]); client.start(); me=client.get_me(); print("登录用户 ID:",me.id); client.disconnect()'
```

手机号、验证码和可能的两步验证密码直接在交互式控制台输入。命令会复用已登录的 `checkin_user.session`；若配置文件位于其他路径，请相应替换命令中的路径。

在配置文件的 `checkin` 中设置 `user_session_name` 为 `checkin_user`，`user_id` 为刚显示的个人数字 ID。重启后，签到模块就会使用该个人会话；启用签到模块并配置目标、Cron 与签到内容。不要让其他进程同时使用同一份个人会话文件。

api_hash、Bot Token、个人会话文件均应保密。公开镜像不含这些内容，每位部署者需自行配置。
