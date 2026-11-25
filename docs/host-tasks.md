# ホスト作業の依頼（host tasks）— 手順 1：registry の 2 段化と `host_probe`

> **実インフラ値**（ホスト名 / IP / パス）は [[platform:infra-registry]] が正本。この文書は `{{PLACEHOLDER}}` で参照する（規約 §3.1）。

設計の正本は chatroom `spirrow-magickit` / `T-host-task-request-with-human-approval`（msg-1750〜msg-1777）。
裁定は msg-1770（Takahito、2026-11-25）。この文書は、その手順 1 の実装で決めたことと、ホストで測ったことの記録。

## 1. 何を解くか

開発環境（`{{HOST_LOOP}}` ほか）から `{{HOST_SERVICES}}` の調査を頼むとき、これまでは Takahito がホストの
Claude Code を起動して中継していた。ssh を開けるのは不可（`{{USER_SERVICES}}` は `NOPASSWD: ALL` で、
秘密の置き場がここにある）。手順 1 は **調査** の中継を要らなくする。設定変更（手順 2 以降）はまだ無い。

| 手順 | 中身 | 状態 |
|---|---|---|
| 1 | registry の 2 段化、`host_probe`（型付き調査・上限・伏せ）、依頼側の説明 | **この PR** |
| 2 | `host_task_request`（型付き env 操作）+ validator + status / history | 未着手（手順 1 が PR gate を通ってから） |
| 3 | 承認の扉 + 適用 runner + restart / health / 退避と復元 | 未着手。停止注入試験（msg-1774 / msg-1776 の全境界）はここ |
| 4 | `host-task` arm（mindwire 側スレッド）、Discord、`expired` | 未着手 |
| 5 | （v2）計画エージェント | 未着手 |

## 2. 形

```
host_task_targets()                        hosts / targets / 登録ファイル / プローブの一覧（名前だけ）
host_probe(host, probe, target, params)    固定のプローブを 1 つ。承認なし
        │
        ▼  hosts.probes.run_probe
  名前の解決（hosts.registry）→ 引数の検証（整数の範囲・登録名）→ 頻度上限（hosts.limits）
        │
        ▼  プローブ関数（固定）
  固定 argv をシェルなしで実行（hosts.execution）／ /proc ／ 登録ファイル（hosts.fs）／ loopback health
        │
        ▼
  出力を値の検証から組み立て直す（hosts.fields）→ 最後の検査（hosts.secretscan）→ 64 KiB に収める
```

- **registry は `host → targets` の 2 段**（R-6）。host の名前は論理名 `services`（= `{{HOST_SERVICES}}`）で、
  実ホスト名ではない。プローブの出力はエージェントの文脈に入り、そこから chatroom や PR 本文に流れるため。
  記録・引数・監査ログには最初から `host` がある。`kind`（`linux-systemd`）と `executor`（`local`）を持ち、
  2 台目は pull 型の executor を足す想定（v1 では実装しない）。
- **deploy 対象と重なる target は、unit と health URL を `deploy/registry.py` から読む**。2 つの表が食い違えない。
- **magickit 自身は target にならない**（R-4、msg-1757 B-9）。名前の接頭辞で、表を見る前に拒否する。

## 3. 「読み取りだけ」を構造で保証する根拠（R-1 / msg-1750 §4 反証 1）

1. プローブは `hosts/probes.py` の `PROBES` にある固定の関数だけ。自由文の調査もエージェントも無い。
2. 引数は registry の名前か、上限付きの整数だけ（`ParamSpec.kind` は `int` と `file` しか無い。テストで固定）。
   パス・unit 名・正規表現・コマンド断片を渡す口が無い。未知の引数は `unknown_param` で拒否する。
3. 実行するのは固定の argv で、シェルを通さない。子の環境は `CHILD_ENV` で置き換え、サービスの環境
   （認証あり面には GitHub PAT がある）を継がせない。stderr は捨てる（自由文で、何でも入りうる）。
4. registry の unit 名は glob 文字・先頭の `-`・`.service` 以外を拒否する（`journalctl -u` と `systemctl` は glob を展開する）。
   health URL は loopback の http だけ（プローブを他ホストへの踏み台にしない）。
5. **出力は値を通さず組み立て直す**（msg-1772 Obj-2-2）。整数は範囲付きで、文字列は閉じた集合か固定形式
   （`MESSAGE_ID` は小文字 hex 32 桁、sha は 40 桁、systemd の状態は短い小文字語）。通らない値は落として
   `omitted` に件数だけ出す。unit 名は journal の欄から取らず、registry の名前を付ける。
6. 最後に `secretscan` が全文字列を見る（Lexora redactor の既知 prefix・Bearer・entropy と、B-2 の 3 規則）。
   **これは境界ではない**（Einstein msg-1758 Obj-3）。形式に収まる値が秘密でないことまでは言えない（msg-1773）。

意図して入れていないもの:

| 入れていないもの | 理由 |
|---|---|
| `systemctl status` | 子プロセスの argv と journal の末尾を出す（msg-1757 B-1）。`show -p <固定一覧>` で代える |
| `systemctl show`（`-p` なし） | `Environment=` を出す |
| journal の `MESSAGE`、ログファイルの tail | 裁定 2（msg-1770）。Takahito が実測を見て決めるまで作らない |
| journal の grep 引数 | 本文を返さない状態で一致件数を返すと、正規表現 1 本ずつ本文を当てる oracle になる |
| 出力中のパス・アドレス・ホスト名 | 上記のとおりエージェント文脈からの流出経路になる |
| 実行されるファイル（`start.sh` 類）の登録 | 変更がそのままコード変更になる（msg-1757 A-1） |
| user unit（`systemctl --user`） | プローブはシステムサービスの中で動き、user manager の bus に届く前提が無い |

## 4. 登録ファイルの開き方（msg-1757 B-6 / msg-1759 Obj-1-6 / 反証 4）

`hosts/fs.py`。`/` から **1 要素ずつ** `openat(O_DIRECTORY|O_NOFOLLOW)` で辿り、各要素を `fstat` で確かめる。
最後のファイルは `O_NOFOLLOW|O_NONBLOCK` で開き、通常ファイルであることとサイズ上限（64 KiB）を開いた fd で確かめる。

- 途中のどこかが symlink なら、辿らずに失敗する。登録後に親ディレクトリを symlink に差し替えられても、
  allowlist の外は開かない（テスト `test_a_parent_swapped_for_a_symlink_after_registration_is_refused`）。
- 各要素の所有者は root か自分で、他人が書けないこと。group 書き込み可は、その group に所有者以外の
  メンバー（補助・主グループの両方）がいないときだけ通す。ホストの `{{USER_SERVICES}}` の group は本人だけ（実測）。
- registry には realpath を載せる。release の `current` や安定 symlink（`{{PATH_SERVICES}}/<svc>`）は経由しない。
  共有設定は `{{PATH_SERVICES}}/releases/<svc>/shared/` にあり、release の切り替えでは動かない（M4 の読み取り側）。
- 手順 3 の書き込みは同じ walk を使い、最後のディレクトリ fd に対して一時ファイルの作成と rename を行う予定。

## 5. 上限（無認証面なので）

| 項目 | 値 | 根拠 |
|---|---|---|
| 全プローブ | 30 回/分・300 回/時（プロセスごと） | 暴走した呼び出し元を CPU 1 コアのごく一部に抑える |
| `journal_summary` | 上に加えて 6 回/分・60 回/時 | 一番重い。2 万件の走査で 1 秒前後（実測） |
| `health` | 12 回/分 | lexora の `/health` は 20〜30 秒止まることがある |
| 同時実行 | 2 | |
| `journal_summary` の走査 | 1 unit あたり新しい順に 20,000 件、20 秒 | 一番多い unit で約 30,000 件 / 52 時間（実測）。超えたら `partial: true` |
| 期間 | 最大 72 時間 | journal は 4G 上限で約 52 時間しか残っていない（実測） |
| 応答 | 64 KiB | 超えたら journal の entries を半分ずつ削り `trimmed_to_fit: true` |
| 1 行 | 4 KiB（journal）。超える行は丸ごと捨てる | 要求したフィールドだけなら 1 行約 450 バイト |
| 登録ファイル | 64 KiB | env ファイルは 1 KiB 未満 |

拒否した呼び出し（名前違い・引数違い）は頻度上限を消費しない。

## 6. ホストで測ったこと（2026-11-25）

| 項目 | 結果 |
|---|---|
| **M1** `systemctl show -p <固定一覧>` | 登録 5 unit すべてで `Environment` 類・パス・argv が出ないことを確認 |
| **M3** journal の読み取り権 | `{{USER_SERVICES}}` は `adm` group にいるので system journal を読める。`-u` で絞った結果に他 unit の行は 0 件（5 unit とも） |
| **M3** 別 unit の詐称 | 非 PID 1 のプロセスが `UNIT=spirrow-lexora.service` を書いても、lexora の `-u` には入らない（host テスト） |
| **M7** 量 | 52 時間で conclair 約 5,500 / lexora 約 600 / cognilens 約 330 / prismind 約 29,500 / vllm-32b 約 1,650 件。priority はすべて 6（stdout 由来） |
| journal の保持 | 4G 上限で約 52 時間分（2026-11-23 早朝〜）。7 日分は rsyslog の `/var/log/syslog*` にしか無い |
| **裁定 2 の実測**（M2 の後継） | 件数だけ。chatroom msg-1778 に報告済み |
| 値の検証（Obj-2-2） | 偽の `MESSAGE_ID`・`PRIORITY`・`EXIT_STATUS` を native protocol で注入し、落ちて件数だけ出ること、本文・自由文の欄が一切出ないことを確認（host テスト） |
| サービスの sandbox の中 | `spirrow-magickit-mcp-local.service` と同じ設定（NoNewPrivileges / ProtectSystem=strict / ProtectHome=read-only / PrivateTmp / MemoryMax=512M）の一時 unit の中で、全 19 呼び出しが成功（host テスト） |

## 7. 手順 2 以降に持ち越すもの

- **M5**（env ファイルの消費者の突き合わせ）: conclair の `.env` は systemd の `EnvironmentFile=` と、アプリの
  pydantic-settings `env_file` の **2 つのパーサ**が読む（実測）。キーを変更可能にする前に、
  msg-1759 Obj-1-2 の型で両方が同じ値を得ることを確かめる。registry の `EnvFile.consumers` に記録済み。
- **M6**（restart 対象が system unit か）: 登録した 5 つはすべて system unit。restart には deploy runner と
  同じ sudo の経路が要る（手順 3 で実測）。
- 登録した env ファイルは今のところ conclair の 1 つだけ。lexora の環境は `/etc` の `EnvironmentFile`
  （root 所有）と実行される `start.sh` から来るので、v1 のファイルにならない。cognilens / prismind に env ファイルは無い。
- `changeable_keys` は手順 2 の型付き allowlist が入るまで空。

## 8. 足し方

- **target**: `hosts/registry.py` の `_SERVICES_TARGETS` に足す PR。unit・health・ポートは現物から読む。
  実行されるファイルと magickit の unit は登録できない（import 時の `validate_host` が拒否する）。
- **プローブ**: `hosts/probes.py` の `PROBES` に足す PR。引数は `int` か `file` だけ。出力は `fields` で組み立て直し、
  `tests/unit/test_host_probes.py` の敵対的な偽ホスト（MESSAGE や Environment を混ぜてくる）で漏れないことを示す。

## 9. ホストでしか回せないテスト

`tests/host/`（gate の `tests/unit` には入らない）。`sudo` で一時的な **system** unit を起動する
（`--user` では sandbox の指定が黙って無視される）。

```bash
MAGICKIT_HOST_TESTS=1 uv run --extra dev pytest -s tests/host/
```

- `test_journal_injection.py` — 偽フィールドの注入と、別 unit の詐称（上の表）
- `test_probes_in_service_sandbox.py` — サービスと同じ sandbox の中で全プローブ
