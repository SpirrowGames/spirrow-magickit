# Deploy runner — magickit から Claude Code を起動して deploy する

## 1. これが埋める穴

PR は `sg-tomtebo-01` からでも merge できる。しかし **live にするのは `sg-ai-server-01` でしかできない** — systemd unit と alembic の履歴がここにあるから。開発ループの走る `sg-tomtebo-01` からここへの ssh は無い。∴「land した」と「動いている」の間に、誰も渡れない川があった。

magickit は既にその境界に立っていて、既にループから到達できている。だから渡し場をここに作る。

## 2. 形

```
  loop (sg-tomtebo-01)                    人間 (claude.ai / OAuth)
        │                                         │
        │ deploy_request                          │ deploy_approve
        ▼                                         ▼
  ┌──────────────────────────────────────────────────────┐
  │ magickit MCP server                                  │
  │  ・NoNewPrivileges=true → sudo 不可 (実測)            │
  │  ・ProtectHome=read-only → repo に書けない (実測)     │
  │  ・書けるのは data/ だけ                              │
  │  ∴ このプロセス自身は deploy を実行できない           │
  └──────────────────────────────────────────────────────┘
        │ systemd-run --user (sandbox の外へ出る唯一の経路)
        ▼
  ┌──────────────────────────────────────────────────────┐
  │ runner (transient unit, 非 sandbox)                  │
  │  lock → pin → backup → [agent] → restart → health    │
  └──────────────────────────────────────────────────────┘
        │ sudo systemd-run --system (再び sandbox の中へ)
        ▼
  ┌──────────────────────────────────────────────────────┐
  │ Claude Code agent (transient unit, sandbox)          │
  │  ・NoNewPrivileges=true / ProtectHome=read-only       │
  │  ・書けるのは対象 repo と自分の scratch だけ          │
  │  ・MCP サーバ無し (--strict-mcp-config)               │
  │  仕事: この repo をこの commit で動く状態にする       │
  └──────────────────────────────────────────────────────┘
```

**手順は magickit が持たない。対象と ref と承認と記録だけを持つ。** repo ごとの deploy の形（`uv sync` が要るか、migration があるか、順序はどうか）は repo の中にあり、magickit に写すとそれは腐る。エージェントは現物を読む。

ただし **restart と health だけは runner がやる**。unit 名は R-4 の都合で元々 registry にあり、`systemctl restart <unit>` は repo 固有の知識ではない ∴ ここに置いても腐らない。そして restart をエージェントの仕事から外すと **エージェントは一切の特権を必要としなくなる** ので、その unit に `NoNewPrivileges=true` を掛けられる。「昇格してはならない」という指示が、カーネルの拒否に変わる。これがこの分割の対価。

## 3. 権限の実測（推測ではない）

`systemd-run` で同条件の unit を作って測った結果:

| 主体 | sudo | 他 repo への書き込み | 備考 |
|---|---|---|---|
| MCP サーバ (system unit, 現行設定) | **不可** `no new privileges flag is set` | **不可** `Read-only file system` | 書けるのは `data/` のみ |
| MCP → `systemd-run --user` した子 | **可** | 可 | sandbox の外。runner はここ |
| runner → `sudo systemd-run --system` した孫 | **不可** | **不可** | agent はここ |

**注意した罠**: `systemd-run --user` では `ProtectHome` / `ProtectSystem` などの sandbox 指定が**黙って無視される**（Ubuntu の unprivileged userns 制限）。実測で user unit からは conclair ツリーに書けてしまった。∴ エージェントの unit は `--system` で起動している。global CLAUDE.md の「長時間プロセスは transient unit + MemoryMax」という要求は満たしているが、scope が `--user` ではない点は意図的な逸脱。

### unit に渡すパスの制約（どちらも「エージェントのせい」に見える壊れ方をする）

- **相対パスは不可**。`ReadWritePaths=data/deploy/runs/x` を渡すと unit は起動すらしない（実測: `Failed to start transient service unit: Invalid ReadWritePaths`）。`systemd-analyze verify` は警告だけで通すので静的検査では捕まらない。∴ `default_state_root()` は必ず絶対パスを返し、`require_absolute()` が unit を組み立てる時点で弾く
- **`/tmp` 配下も不可**。`PrivateTmp=true` が namespace 内でそのパスを消すため、実行前に **exit 226 (EXIT_NAMESPACE)** で死ぬ。本番の対象は全て `/home/sgadmin/services/spirrow` 配下なので発火しない ∴ 致命化せず警告に留め、代わりに **exit 226 を「これはパスの問題であってエージェントの問題ではない」と名指しで報告**する

どちらも放置すると「エージェントがレポートを書かなかった」という**原因から 3 層離れた症状**として現れる。

### git は ignore されたファイルを黙って上書きする

commit が「ここでは ignore されているパス」を**追跡し始める**と、fast-forward merge はホスト側のコピーを**警告なし・exit 0 で置き換えます**（実測）。ignore されたファイルは「作業ツリーが汚れている」判定にも出ないので、pin のクリーンチェックも素通りします。

これは仮定の話ではありません。**lexora / cognilens / prismind は systemd が実行する `start.sh` 自体を ignore しています** ∴ 誰かがそれを upstream に commit した瞬間、次の deploy が**動いているサービスの起動方法を黙って書き換え**、結果は成功と報告されます。

∴ pin は、そうなるファイルを blob hash で検出して**拒否**します（内容が同一なら失われるものが無いので通す）。実リポジトリ 4 件で誤検知が無いことは確認済み。

なお `start.sh` を git 管理下に移すのは**やりません**。上の事故を招くうえ、release 方式では `shared/` に置けば済み、lexora の絶対パス直書き (`cd /…/spirrow-lexora`) は**そのパスが symlink になるのでそのまま正しく動く**からです。ただし「動いているものが git で完全には説明できない」状態は残ります（`deployed_sha` は起動条件を含まない）。

### health check は本文を読んではいけない

cognilens と prismind に平の health エンドポイントは無く（`/health` `/healthz` `/api/health` `/` すべて 404、実測）、答えるのは MCP の SSE mount。**SSE の本文は終わらない** ∴ `httpx.get` は健全なサービス相手に必ずタイムアウトする（実測: 両方とも 8s で ReadTimeout、`httpx.stream` なら 0.0s で 200）。runner は**ステータス行だけを見る**。

lexora の `/health` は 20〜30s 詰まることがある ∴ 1 回あたりのタイムアウトは 40s。短くすると「遅い」が「失敗」になる。

### 通しの実測（ダミー repo、本番非接触）

- sandbox 付き unit で Claude Code が起動し、手順を自力で判断し、レポート JSON を書いて返すところまで確認
- `git checkout -b` は deny され、ブランチは作られなかった
- 手順が矛盾する repo（CLAUDE.md 無し・排他的な lockfile 4 種・空の migration ディレクトリ）では
  **`undetermined: true` で何もせず停止**した（Q-4 の要求どおり）。指定された commit が存在しない
  ことにも気づいて報告した
- **本番と同じ形の state root**（`MAGICKIT_DEPLOY_STATE_DIR` 未設定・相対デフォルトを絶対解決）で
  再実行し、scratch 作成・レポート書き込み・回収まで通ることを確認。以前のスモークはここだけ
  絶対パスを直接渡していたため、上記の相対パス欠陥を素通りさせていた
- **診断パス**は read-only sandbox（`ReadWritePaths` から repo を外す）で実際に診断文を返し、
  同時に repo への書き込みは拒否された（`git status` clean のまま）

## 4. 制約への対応

| | どう満たしたか |
|---|---|
| **R-1** ref は `origin/main` のみ | `registry.DEPLOY_REF` という**定数 1 個**と、`deploy_request` に **ref 引数が無い**こと。検証ではなく不在。人間の override は承認側にだけあり、理由必須で監査に載る |
| **R-2** migration は硬く | ① backup を **runner が無条件に**取る（エージェントの勤勉さに依存しない）② `HEAD == origin/main` でなければ gate を閉じる ③ ref override は migration を自動では解禁しない（`override_allows_migration` が別途必要）④ gate が閉じているとき、**未適用 migration を持つ commit は deploy 自体を拒否**する（下記）⑤ **alembic の revision を前後で読み**、gate が閉じているのに動いていたら deploy を失敗にして restart しない |
| **R-3** 人間の承認なしに実行不可 | `deploy_request`（記録するだけ）と `deploy_approve`（実行を起こす）を別 tool にし、**approve は認証済みインスタンスにしか登録しない**。ループ側の tool 一覧に存在しない |
| **R-4** 対象は allowlist | `registry._TARGETS`。Python に置いたのは、これが境界だから — 変更は PR を通る。`spirrow-magickit` は**別の分岐で先に拒否**（表に足しても解禁されない） |
| **R-5** 道具を絞る | §5 |
| **rollback** | 承認付き `deploy_rollback`。呼び出し側は **commit ではなく「取り消したい過去の deploy」を名指し**し、sha は magickit の記録から読む ∴ R-1 の抜け道にならない。migration を当てた deploy は拒否 |
| **R-6** 構造化された結果 | `DeployResult`。`deployed_sha` は deploy 後に **git から読み直す**（エージェントの自己申告ではない）∴「deploy された sha == merge された sha」の機械照合が意味を持つ |
| **R-7** 失敗は大きな声で | `service_state` が `running_new` / `running_previous` / `running_unknown_version` / `down` / `unknown` を区別する。「deploy が失敗した」と「何も動いていない」は別の語 |
| **R-8** 監査ログ | `data/deploy/audit.jsonl` に append-only。`deploy_history` tool で**リモートから読める** ∴ 失敗調査に ssh が要らない |
| **R-9** 同時実行防止 | `flock`。status フィールドではないのは、プロセスが死んでも status は「真」のままだから。中断は「ロックが空いている＝runner はもういない」を根拠に次の runner が `interrupted` に落とす。タイムアウトも polling も無い |

### migration を走らせるのはエージェントだけではない

conclair の unit は `ExecStartPre=.../alembic upgrade head` を持つ ∴ **サービスを再起動するだけで、ツリーにある migration が当たる**。エージェントに alembic を deny しても、systemd がその gate を素通りする。

∴ gate が閉じているときは、**未適用の migration を持つ commit をそもそも deploy しない**（backup も agent も restart も始まる前に停止）。`alembic current` と `alembic heads` を比べて判定する。これは安全側であると同時に正直でもある — migration を必要とするコードを migration 無しで出すのは、どのみち壊れている。

（`alembic` が無い対象、revision が読めない対象は「pending が証明されていない」として扱い、deploy 不能にはしない。）

## 5. R-5: 何を許可し、何を許可しなかったか

**本当の境界（カーネル）**

- `NoNewPrivileges=true` — これが要。sgadmin は `NOPASSWD: ALL` なので、**sgadmin として無拘束に走るエージェントは実質 root** であり、どんな禁止コマンド一覧もそれを変えない
- `ProtectHome=read-only` + `ReadWritePaths` は対象 repo と自分の scratch と `~/.claude` のみ — 他サービスのツリーを触れない ∴ 指された対象以外を deploy できない
- `--strict-mcp-config` で MCP サーバをゼロにする — magickit 自身の tool に届かない（自分の deploy を承認する、が明らかな危険）
- `PrivateTmp` / `MemoryMax=4G` / wall-clock timeout

**ガードレール（Claude Code の deny 規則）**

- ref を動かす git（`checkout` / `merge` / `fetch` / `reset` / `push` …）— R-1 の二枚目。読み取り系 git は許可（何を deploy するのか見る必要がある）
- `sudo` / `systemd-run` / **状態を変える `systemctl` の verb のみ**（`start` / `stop` / `restart` / `enable` / `mask` / `daemon-reload` …）— unit 側で既に不可能だが、明示的に deny すると**試みが `permission_denials` として結果 JSON に載り、監査記録に残る**。
  `systemctl status` / `show` / `is-active` などの**読み取りは許可**する。診断 brief がそれを見ろと指示しているのに全面 deny していたのは矛盾で、スモーク実行時に**エージェント自身が指摘した**（「brief は systemctl status を使えと言うが sandbox が全面拒否している」）。読むのに特権は要らず、変えるには sudo が要る
- gate が閉じているときの alembic 各種

**deny-list 方式であることは明示的に指定している**。`permissions.allow` を空にすると headless では確認相手が居ないので **Bash が全部拒否される**（実測: スモーク実行で `git ls-tree` も `printenv` も `python3 -c` も拒否され、エージェントは自分のレポートすら書けなかった）。加えて、未知の repo の手順を判断させる以上、事前に列挙できないコマンドを走らせる必要がある ∴ ツールは丸ごと allow し、危険な綴りを deny する。**deny が allow に優先する**ことは実測済み（`git checkout -b probe-branch` が拒否され、ブランチは作られなかった）。

**正直な限界**: deny 規則は「禁じた物の列挙」であり、shell には無限の言い換えがある ∴ **これは境界ではない**。だから migration については deny に頼らず revision の前後比較で**検出**する。そして deploy が生き延びられない事（特権・他 repo）はカーネルが拒否する。

**未実装の強化案**: 専用 unix ユーザ `spirrow-deploy` を作り、sudoers を restart だけに絞る。今は sgadmin で走るので、上記の隔離は「sgadmin として何ができるか」を狭めているだけで、`~/.claude` への書き込みは開いている（Claude Code の session state に必要）。これを閉じるには専用ユーザと専用 credential が要る。root 権限の構成変更なので本 PR には含めない。

```
# 将来 /etc/sudoers.d/spirrow-deploy として入れる案（未適用）
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart spirrow-conclair.service
spirrow-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl is-active spirrow-conclair.service
```

## 6. 設計判断（Q-1〜Q-4）への回答

### Q-1 認証 — 現状維持。ただし「無認証面は要求のみ」を構造で保証

無認証 tailnet 面から呼べるのは `deploy_request` / `deploy_status` / `deploy_history` / `deploy_targets` だけ。**`deploy_approve` はそのインスタンスに登録されない**（拒否するのではなく存在しない）。

∴ 無認証の扉の向こうにある能力は「行を 1 本書く」のままで、サービス再起動にも migration にも届かない。ADR の再判断条件（能力の拡大）には**当たらない**、と積極的に論証できる形にした。実際に増えた能力（restart と migrate）は認証済みの扉の内側にある。

コスト 0、残るリスクは「要求でキューが荒らされる」程度。

### Q-2 承認の表現 — 要求と承認を別 tool にし、承認は OAuth 面限定

新しい認証機構は作っていない。**既に走っている 2 インスタンスの違いがそのまま承認境界になる**:

- `spirrow-magickit-mcp.service` — Google OAuth 済み → `deploy_approve` **あり**
- `spirrow-magickit-mcp-local.service` — `MAGICKIT_AUTH_DISABLED=1`、tailnet、ループが使う → **なし**

「人間が承認した」＝「あなたが誰か知っている扉を通ってきた」。`approved_by` は credential ではなく記録で、認証しているのは扉の方。

### Q-3 rollback — 承認付きの `deploy_rollback`。自動では動かない

「deploy が失敗した」と「成功したが動作がおかしい」は別問題で、後者は機械には判定できない ∴ **自動 rollback は入れない**。前者の報告は `service_state` / `health_ok` / `deployed_sha` / `diagnosis` が担う。

後者のための専用経路が `deploy_rollback`。deploy と同じ **要求 → 人間の承認 → 実行**を通り、同じ lock・backup・health check を通る。

**呼び出し側は commit を名指ししない。** 「取り消したい過去の deploy」を名指しし、戻る先の sha は magickit がその deploy の記録 (`previous_sha`) から読む ∴ rollback は R-1 の抜け道にならない。ツリーはその sha に detached で checkout され、migration gate はその run の間ずっと閉じている。

**migration を当てた deploy の rollback は拒否する。** コードは古い commit を入れ直せば戻るが、スキーマは戻らない。migration より前のコードが、それを適用済みの DB に出会うのは rollback ではなく 2 件目の事故。その場合の復旧は人間の判断で、`data/deploy/audit.jsonl` の当該 deploy の時刻と、conclair の `backups/` にある**その deploy の直前の snapshot** を突き合わせる。復元はその間に書かれたデータを捨てるので最後の手段。

### Q-4 手順を判断できなかった場合 — 止まって報告

brief に明示してある: 推測するな、それらしいコマンドを試すな、「いつものやつ」をやるな。`undetermined: true` で報告して止まる。結果は `error` に「エージェントがこの repo の deploy 手順を判断できず、何もせずに停止した」と出て、**restart は起きない**。

何もしないで止まった deploy は良い結果で、推測で半分進んだ deploy がこの仕組み全体の防ごうとしているもの。

## 7. 使い方

```
# ループ側（無認証 tailnet 面）
deploy_targets()   # spirrow-conclair / lexora / cognilens / prismind
deploy_request(target="spirrow-conclair", requested_by="mindwire-conductor",
               reason="conclair#10 が merge 済みだが thread listing は旧順序のまま")
  → {"request_id": "…", "status": "pending_approval"}

# 人間側（claude.ai / OAuth 面）
deploy_status(request_id="…")        # 何を頼まれているか読む
deploy_approve(request_id="…", approved_by="Takahito")
  → {"status": "running", "unit": "magickit-deploy-…"}

# どちらからでも
deploy_status(request_id="…")        # 数分後、結果
deploy_history(limit=50)             # 監査ログ

# 出したが動きがおかしい → 取り消しを要求（これも承認が要る）
deploy_rollback(request_id="…", requested_by="…", reason="…")
```

閲覧だけなら `/dashboard/deploys`（tailnet の magickit web）。**読み取り専用**で、承認ボタンは無い — 無認証面からサービス再起動に届かせないため。

## 8. まだやっていないこと

- **magickit 自身の deploy** — 意図的な carve-out として残す。**当初ここに書いていた理由（「runner が restart で死ぬ」）は実測で誤り**だった: runner は `user@1000.service/app.slice` の user transient unit で、起動元の system サービスを止めても生き残る（実測）。実際には deploy は完走し結果も記録される。
  残る理由はもっと小さい: **失敗したときに何が起きたかを報告する tool 自身が落ちている**。`deploy_status` も `deploy_history` も再起動される MCP サーバが答えるので、magickit だけは「失敗の調査にホストへの到達が要る」— この機能が消そうとしている依存そのものになる。解くには magickit に依存しない報告経路が要る（allowlist の項目ではない）
- **専用ユーザによる隔離** — 手順と成果物は [`docs/deploy-hardening.md`](deploy-hardening.md) に用意済み。**未適用**。本当の障害物は sudoers ではなく Claude の認証で、`~sgadmin/.claude/.credentials.json` は別ユーザから読めない ∴ API キーか専用ユーザでの対話ログインのどちらかが要る（2026-08-16 時点では現状維持を選択）
