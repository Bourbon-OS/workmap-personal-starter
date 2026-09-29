# Work Map 個人利用・開発スターター

自分の仕事の目的・現在地・判断理由を保ち、会社側で育てるためのソース付き導入版です。完成した画面付き製品ではありません。新しい空の正本から始めます。元の開発プロジェクトの仕事・会話・実データは含めていません。

## 最初にすること

1. ZIPを会社PCの**同期対象外のローカルフォルダ**へ展開します。例：`C:\WorkMapPersonal`。OneDrive、共有フォルダ、ネットワークドライブ上では運用しません。
2. PowerShellでそのフォルダを開き、`python --version` で **Python 3.11以上**を確認します。追加ライブラリのインストールは不要です。
3. 次の例を自分の仕事の言葉へ直して実行します。本人の登録指示として扱われるので、AIに実行させる場合も実際に承認した内容を渡してください。

```powershell
python workmap.py init --name "自分の仕事" --intent "担当業務の目的と判断理由を保ち、確実に引き継げるようにする" --origin "自分の仕事でWork Mapを使い始める" --owner-instruction "この名前・目的・由来で、自分の仕事の親Workを登録する"
python workmap.py show
```

最初の親はW0です。登録する目的は自由に決めてください。初期化の再実行は既存DBを上書きせず拒否します。

## 仕事を登録する

```powershell
python workmap.py add --parent W0 --name "月次報告を整える" --intent "報告を受ける人が状況と必要な判断を理解できるようにする" --origin "月次報告の担当になった" --owner-instruction "この仕事をW0の子として登録する"
python workmap.py show
python workmap.py show --work W1
python workmap.py thread --work W1
```

`--parent` は現在Mapで確認した住所を指定します。内部IDと空き住所、revision、競合検査、保存、読戻しは既存入口が扱います。登録だけでは担当Pinや実作業Permissionを追加しません。

「後で行う」と決めた仕事も今登録します。後でやめるなら理由を残して閉じます。Intent／Originを意味Threadに重複させません。出生時にそれだけでは残せない重要な理由がある場合だけ `add --reason "理由"` を付けられます。

## 現在地と理由を読む

- `show`：現在の仕事の木。
- `show --work W1`：目的、親、関与、許可、制約、依存、理由など。
- `thread` または `thread --work W1`：将来の判断に必要な意味だけ。
- `history --work W1`：操作の技術履歴を明示的に確認するとき。

通常の登録直後にThreadが空でも異常ではありません。出生時の理由はWorkのIntentとOriginにあります。unknownや読取不能を、空集合・未実行・成功と決めつけません。

## 応答が分からなくなったとき

新しい登録をやり直す前に `show` で現在Mapを確認します。登録要求は `data/transition-requests/` に保存されます。同じ要求を再確認します。

```powershell
python workmap.py resume-add --request "data/transition-requests/実際の要求ファイル名.json"
```

確認できない場合はunknownのまま、Map・履歴・Owner指示を照合します。DBや履歴を手編集して成功扱いにしません。

## バックアップと復旧確認

新しい保存先フォルダを作って実行します。

```powershell
New-Item -ItemType Directory backups/day1
python workmap.py backup --output backups/day1/backup.json
```

出力されたSHA-256も保存してください。`backup.json`と同じ場所へ自動保存されたThread選別資料は、一緒に保管します。**別途、このスターターのソースと `docs`、`data/transition-requests` も保全してください。** DBのバックアップには、リンク先の社内文書や外部Artifactの実体は含まれません。バックアップは編集する第二正本ではありません。

```powershell
python workmap.py recover-check --backup backups/day1/backup.json --sha256 "出力されたSHA-256" --target recovery-check/candidate.sqlite3
```

これは隔離した復旧候補の確認です。現用DBを自動で置き換えません。実際の復旧切替は、現在値・候補・必要な外部根拠を照合してから、同梱Coreの切替経路で別途判断します。

## 会社側で育てる人へ

[HANDOVER.md](HANDOVER.md)に設計の要点・ファイル配置・残る実装範囲があります。日常操作すべての簡単な入口、人間向け画面、チーム共有はこのスターターでは未完成です。既存CoreにはPin、Permission、意味理由、close/reopen等の操作がありますが、その呼出しには操作ごとの根拠確認が必要です。手組みの更新要求が当面必要な部分を、会社側の開発で既存Coreへつなげてください。

この配布は個人利用から始めるための限定スターターです。会社PCでの実起動・利用条件は会社側で確認してください。全社導入の適合確認はしていません。利用後に作られる実Work・根拠・バックアップを公開リポジトリへコミットしないでください。

## ライセンス

コード等は[MIT](LICENSE)、説明文書は[CC BY 4.0](LICENSE-DOCS.md)です。適用範囲と著作者表示は[NOTICE](NOTICE.md)、初期版の到達範囲は[リリースノート](RELEASE_NOTES.md)を参照してください。
