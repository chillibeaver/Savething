# Savething

告别在每台设备上手动往 Syncthing 里复制粘贴存档路径。[English](README.md)

Savething 用 [Ludusavi](https://github.com/mtkennerly/ludusavi) 找到每个游戏的存档目录，用 [Syncthing](https://syncthing.net) 在 Windows 和 SteamOS 之间同步。一台常开的服务器负责中转，所以各台设备不需要同时开机。

每个游戏只需在一台设备上 share 一次，在其他设备上 accept 一次，之后交给 Syncthing。

---

## 前置要求

- **一台常开的服务器**，运行 Syncthing
- **Windows**/ **SteamOS**游戏设备，装好 Syncthing。SteamOS 上支持哪些游戏见 [SteamOS](#steamos)。
- **Ludusavi**：发起共享的设备需要。只接收存档的设备不用装。

---

## 安装

### 1. 服务器

在服务器的 Syncthing 网页里打开 **操作 → 设置 → 图形用户界面**：

- 把 **监听地址** 改成 `0.0.0.0:8384`，让各台设备能访问 API。
- 复制 **API 密钥**。

然后建一个 Syncthing 有写权限的目录用来放存档，例如 `/srv/media/game/savething`。

### 2. 配对设备

在 Syncthing 里把每台游戏设备和服务器互相添加，游戏设备之间也互相添加。等它们都显示 **已连接**。

如开启自动接收则需要取消此选项: **共享** 选项卡 → 取消勾选 **自动接受**。否则已经取消共享的游戏可能会自己回来。

设备将按 Syncthing 里的名字显示。若想隐藏某些设备，可把名字规则写进 `config.json` 的 `exclude_device_patterns`。

### 3. 初始化每台游戏设备

双击 `savething.exe`，选 `6. init`，依次输入服务器上的存档目录、地址（如 `http://192.168.1.10:8384`）和 API 密钥。或者一条命令写全，不再提问：

```
savething.exe init --server-url http://<服务器IP>:8384 --server-key <API 密钥> --server-root <存档目录>
```

存档目录是默认的 `/srv/media/game/savething` 时可以不写 `--server-root`。

看到 `Init complete.` 就完成了。运行 `5. status` 检查连接，再运行 `2. preview` 看看会同步哪些内容。

---

## 使用

双击 `savething.exe` 打开菜单，或者直接运行命令：

| 命令 | 菜单 | 作用 |
|---|---|---|
| `share` | 1 | 选择要同步的游戏和设备 |
| `share --dry-run` | 2 | 只预览，不做任何改动 |
| `accept` | 3 | 接受共享给本机的存档 |
| `unshare` | 4 | 在所有设备上停止同步某个游戏（文件保留） |
| `status` | 5 | 查看同步状态 |
| `init` | 6 | 初始化本机 |

**`share` 选项**

- `--all`：也显示被排除的游戏（比如没有存档文件的游戏）。在菜单里用 share 时，在选游戏的提示处输入 `showall` 即可。
- `--devices <名字>`：直接指定目标设备，不再询问。
- `--yes`：跳过确认。不加 `--include-config` 时不同步设置文件。
- `--include-config` / `--exclude-config`：同步或不同步设置文件，不再询问。

**`unshare` 选项**

- `--game <ID 或游戏名>`：直接指定游戏，不显示列表。
- `--yes`：跳过确认。

---

## 工作原理

`share` 向 Ludusavi 查询游戏的存档位置。Savething 在本机和服务器上建好共享，并把存档路径按用户目录记进 `savething-registry` 文件夹，比如 `<home>/Saved Games/Hades II`。在另一台设备上，`accept` 换上本机的用户目录。之后的同步全部由 Syncthing 完成。

在 SteamOS 上，Proton 前缀里的 `drive_c/users/steamuser` 就是用户目录，所以同一个路径也能用：

```
Windows：    C:/Users/you/Saved Games/Hades II
SteamOS：    .../compatdata/<appid>/pfx/drive_c/users/steamuser/Saved Games/Hades II
```

---

## 须知

- **被排除的游戏：** 只把存档写在 Windows 注册表里的游戏，以及存档只在 Steam 或安装目录里的游戏（比如 Steam 云存档的 `userdata`，这部分交给 Steam 云）。Steam 游戏和其他游戏一视同仁。
- **`[ignored in Ludusavi]`** 表示这个游戏在 Ludusavi 的备份页里没有勾选。它只是个提示，游戏照样可以共享和同步。
- **设置文件：** 画面和按键设置默认不同步，便于每台设备保留自己的设置。`share` 时会逐个游戏询问。
- **存档分散在几个地方的游戏**，每个位置建一个同步文件夹。
- **已有存档：** 如果设备上已经有这个游戏的存档，较新的文件胜出，较旧的保留为 `*.sync-conflict-*`。
- **备份：** 服务器为每个存档文件保留最近 10 个版本，放在游戏目录下的 `.stversions` 里。
- **Unshare** 会保留所有存档文件。其他设备下次运行 `accept` 时停止同步。
- **不要删除 `savething-registry` 文件夹。** `accept` 靠它知道每个游戏的存档该放哪。

## SteamOS

支持：**用 Proton 运行的 Windows 游戏**，Steam 游戏和以非 Steam 游戏身份加进 Steam 的游戏都行。原生 Linux 游戏不支持。在桌面模式下打开 Konsole，运行：

```
curl -OL https://raw.githubusercontent.com/chillibeaver/Savething/refs/heads/main/savething.py
python3 savething.py
```

- **安装：** 在 Discover 里装 Ludusavi 和 SyncThingy（Syncthing），都会被自动找到。
- **`accept` 之前先启动一次游戏**，让 Proton 建好前缀。
- **Steam 游戏**自动使用它自己的 Proton 前缀。
- **快捷方式叫什么都行。** 对非 Steam 游戏，Savething 会自己找到运行这个游戏的快捷方式；认不出来时问你一次，之后记住。
- 只有 C 盘上的存档能对应过来，其他盘上的会跳过。

---

## 常见问题

**`accept` 什么都没找到，但 Syncthing 里有邀请**
等几秒让登记表从服务器同步过来，再运行一次 `accept`。

**“Cannot reach server Syncthing”**
在这台设备的浏览器里打开 `http://<服务器IP>:8384`。打不开的话，检查服务器的监听地址和防火墙。

**“The server Syncthing does not know this device yet” / “has not added the server device yet”**
这台设备和服务器没有在 Syncthing 里配对。

**`share` 的设备列表里少了某台设备**
本机 Syncthing 里没添加它，或者它的名字匹配了 `exclude_device_patterns`。

**“Cannot find ludusavi”**
在 `%APPDATA%\savething\config.json` 的 `"ludusavi"` 一项写上完整路径，比如 `"C:\\Tools\\ludusavi.exe"`。

**“Cannot find the local Syncthing config.xml”**
运行 savething 时加 `--st-home <Syncthing 配置目录>`。

**Windows SmartScreen 拦截 savething.exe**
exe 没有签名。点 **更多信息 → 仍要运行**，或者改用 `python savething.py`。

**更换服务器地址或 API 密钥**
重新运行 `init`，或者编辑 `%APPDATA%\savething\config.json`。
