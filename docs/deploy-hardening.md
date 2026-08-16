# deploy エージェントを専用ユーザに分離する（未適用の手順書）

**状態: 未適用（§2.5 の決定により当面やらない）。** 現在エージェントは `sgadmin` で走る。適用は root 権限の構成変更で、かつ下の §3 の認証の決着が要るので、手順と成果物だけをここに置く。

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

## 2.5 決定: この分離は当面**やらない**（2026-08-16、Takahito）

**状態: 意図的に受容。** 以下は「まだ手が回っていない」ではなく「読んだうえで見送った」項目。

### 何を受容したのか

デプロイエージェントは `sgadmin` として動く。書き込みはカーネルが封じている（`NoNewPrivileges` で
sudo 不可、`ReadWritePaths` は待機面と scratch のみ）が、**読みは sgadmin の届く範囲すべてに及ぶ**:

```
~/.claude/.credentials.json          Claude の OAuth トークン
services/infra/.env                  postgres スーパーユーザのパスワード
magickit/oauth.env, github.env       Google OAuth の秘密、GitHub PAT
prismind shared/credentials.json     Google OAuth クライアントシークレット
conclair shared/.env                 DB 接続情報
```

外向きのネットワークも開いている（API 呼び出しに必須なので塞げない）。

成立する連鎖は具体的にこうなる: **`origin/main` に混入した悪意あるテキスト → エージェントがそれを
読む（repo を読んで手順を判断するのが仕事なので、repo の中身は入力）→ 資格情報を読み出して外部へ**。
`main` に載せられる主体には自律ループも含まれる。

### 受容の論拠

**`main` に何かを載せられる時点で、守るべきものはほぼ突破されている。** main のコードはそのサービス
*として*動くので、`conclair/.env` も prismind の OAuth トークンも、エージェント経由でなくとも
手に入る。そこから一歩進まれても被害は大差ない。

この論拠は上記のうち **1 つを除いて**成立する。

### 受容していない部分（例外）

**Claude の OAuth トークンだけは、どのサービスも保持していない。** main に載った悪意あるコードが
conclair として動いても手に入らない唯一の資格情報であり、被害範囲が**このホストの外**（アカウント
そのもの）に出る。デプロイエージェントは各サービスの権限を**横断して集約**している、というのが
この構成の正確な description であって、「main が破られたら同じ」は per-service には正しいが
aggregate には正しくない。

これは分かったうえで受容している。

### 再判断の条件

以下のいずれかが起きたら、この決定は前提を失うので見直すこと:

1. **デプロイ対象に、`main` のレビュー水準が同じでない repo が入る**（外部由来、あるいは
   レビューなしでマージされる repo）。受容の論拠は「main に載るものは信頼できる」に依存している
2. **Claude の資格情報が単一ユーザのものでなくなる**（組織共有・チーム席など）。被害範囲が
   あなた個人からチームに広がると、集約のコストが変わる
3. **`main` への書き込み権を持つ主体が増える**（人・ループを問わず）
4. **エージェントに与える repo の数が増え、横断して読める秘密がさらに増える**

### 今できる安価な緩和（未実施）

境界にはならないが、可視化にはなる:

- brief に「資格情報を読むな」と書く。守らせる効果はないが、**試みが `permission_denials` として
  結果 JSON と監査ログに残る**
- `Bash(cat *credentials*)` 等を deny 一覧に足す。同上

## 3. やる場合に決着が必要なこと — エージェントの Claude 認証

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
