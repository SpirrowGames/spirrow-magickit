# deploy エージェントを専用ユーザに分離する（未適用の手順書）

**状態: 未適用。** 現在エージェントは `sgadmin` で走る。適用は root 権限の構成変更で、かつ下の §3 の認証の決着が要るので、手順と成果物だけをここに置く。

## 1. なぜ要るのか — 今の隔離がどこまでで、どこからが効いていないか

`docs/deploy-runner.md` §5 のとおり、エージェントは system transient unit の中で動き、**カーネルが実際に拒否している**ものがある:

| 効いているもの（実測） | 効いていないもの |
|---|---|
| `NoNewPrivileges=true` → `sudo` が一切通らない | `~/.claude` への書き込み（Claude Code の session state に必要なので開けてある） |
| `ProtectHome=read-only` + `ReadWritePaths` → 対象 repo と scratch 以外に書けない | `sgadmin` の**読み取り可能な資産すべて**（`.credentials.json`、`/etc/spirrow-*.env` 等は unit の外から読めば読める…わけではないが、`ProtectSystem=strict` は読みを止めない） |
| MCP サーバをゼロにする → magickit 自身の tool に届かない | |

要点は 1 つ: **`sgadmin` は `NOPASSWD: ALL` を持つ**。今のところ `NoNewPrivileges` がその行使を止めているが、止めているのは *unit の設定 1 行* であって、ユーザの権限そのものではない。unit から外れた経路（誰かが手で起動する、将来の変更で property が落ちる）ではその 1 行が消える。

専用ユーザにすると、**止めているものが「設定」から「その uid にそもそも権限が無いこと」に変わる**。

## 2. 成果物

### 2.1 ユーザ作成

```bash
sudo useradd --system --create-home --home-dir /var/lib/spirrow-deploy \
     --shell /usr/sbin/nologin spirrow-deploy
# 対象 repo を読み書きさせるため、所有者グループに入れる
sudo usermod -aG sgadmin spirrow-deploy
```

`services/spirrow/*` は `sgadmin:sgadmin` の 775 なので、group 経由で書ける。**ここが妥協点**: group 参加は対象 repo 以外の sgadmin 資産にも group 読みを与える。より締めるなら repo ごとに ACL (`setfacl -m u:spirrow-deploy:rwx`) を張り、group 参加はしない。

### 2.2 sudoers（restart のためだけ）

```
# /etc/sudoers.d/spirrow-deploy  (mode 0440)
# deploy エージェントは restart を必要としない設計 (runner がやる) が、
# runner を分離する場合に備えてこの形を置く。verb と unit を固定する。
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart spirrow-conclair.service
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart spirrow-lexora.service
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart spirrow-cognilens.service
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart spirrow-prismind.service
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl is-active spirrow-*.service
```

`visudo -c -f /etc/sudoers.d/spirrow-deploy` で検証してから配置する。

### 2.3 エージェント unit の変更

`magickit/deploy/agent.py` の `build_argv` が組み立てる `--uid` / `--gid` を差し替える。

```python
AGENT_UID = os.environ.get("MAGICKIT_DEPLOY_AGENT_UID", "sgadmin")
```

`MAGICKIT_DEPLOY_AGENT_UID=spirrow-deploy` を MCP unit の `Environment=` に足し、`launcher.py` の passthrough 一覧にも足す（**足し忘れると黙って無視される** — 一度やった）。

## 3. 決着していないこと — エージェントの Claude 認証

**これが本当の障害物。** 現在の資格情報は `/home/sgadmin/.claude/.credentials.json` (mode 600, `sgadmin` 所有) にあり、**別ユーザからは読めない**。`--bare` は OAuth を読まず `ANTHROPIC_API_KEY` か `apiKeyHelper` のみを見る。∴ 選択肢は 2 つしかない:

1. **API キー** — `spirrow-deploy` の unit に `EnvironmentFile=/etc/spirrow-deploy/anthropic.env` (mode 0400, 所有 `spirrow-deploy`) で `ANTHROPIC_API_KEY` を渡す。分離は完全になるが、キーの発行・失効・課金経路が別に増える
2. **専用ユーザで対話ログイン** — `sudo -u spirrow-deploy -H claude` を一度手で走らせて OAuth を通す。キー管理は増えないが、**人間の手作業**が要り、資格情報の期限切れごとに再発生する

2026-08-16 時点では **(現状維持) を選択済み** — エージェントは `sgadmin` のまま、上の分離は未適用。

## 4. 適用したら確認すること

```bash
# エージェント uid が変わったか
sudo systemd-run --system --wait --pipe --uid=spirrow-deploy /usr/bin/id -un

# sudo が絞れているか（1 番目は通り、2 番目は拒否されるべき）
sudo -u spirrow-deploy sudo -n /usr/bin/systemctl restart spirrow-conclair.service
sudo -u spirrow-deploy sudo -n /usr/bin/systemctl stop spirrow-conclair.service   # 拒否されること

# 資格情報が読めなくなっていること（分離の要点）
sudo -u spirrow-deploy cat /home/sgadmin/.claude/.credentials.json   # Permission denied
```

最後の 1 行が通ってしまうなら分離できていない。
