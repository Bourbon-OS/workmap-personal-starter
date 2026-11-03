# Work Map／Work Threadを使い始める

考え方と一続きの利用例は[README](README.md)にあります。この資料は、初期設定と必要な操作を調べるための手順です。毎回全文を読む必要はありません。

## 最初にすること

1. ZIPを利用するWindows PCの**同期対象外のローカルフォルダ**へ展開します。例：`C:\WorkMapPersonal`。OneDrive、共有フォルダ、ネットワークドライブ上では運用しません。
2. PowerShellでそのフォルダを開き、`python --version` で **Python 3.11以上**を確認します。GUIにはPython標準のTkinterが必要です。追加ライブラリのインストールは不要です。短いフォルダ名を勧めます。
3. 次の例を自分の仕事の言葉へ直して実行します。本人の登録指示として扱われるので、AIに実行させる場合も実際に承認した内容を渡してください。

```powershell
python workmap.py init --name "自分の仕事" --intent "担当業務の目的と判断理由を保ち、確実に引き継げるようにする" --origin "自分の仕事でWork Mapを使い始める" --owner-instruction "この名前・目的・由来で、自分の仕事の親Workを登録する"
python workmap.py show
```

最初の親はW0です。登録する目的は自由に決めてください。初期化の再実行は既存DBを上書きせず拒否します。

## GUIで開く

最初に、このMapへの新規登録と、親Work自身の編集等をOwnerに許可します。実際に許可する内容だけを指定してください。

```powershell
python workmap.py permit --work W0 --map-add --owner-instruction "このMapの現在・今後のWorkの下への新しいWork追加をOwnerに許可する"
python workmap.py permit --work W0 --edit --record --pin --close --owner-instruction "W0の訂正、理由と結果の記録、本人Pin操作、通常契約を満たす場合の終了をOwnerに許可する"
python workmap.py gui
```

次からは `WorkMap.pyw` のダブルクリックでも開けます。現在Map・担当別・Work詳細・Work Threadを見て、許可された名前・目的・出自の訂正、新Work追加、任意の自動更新ON/OFFができます。変更時の確認画面と既存Coreの許可・競合・根拠版検査を使います。Pin操作やcloseは下記のコマンドです。

GUIの訂正・追加では、今回の判断を支える資料をこのフォルダの `docs` 配下へ保存し、選択します。目的の訂正・明確化には `docs/work-management/evidence/owner_任意名.txt` を使い、今回のOwner判断、変更前後、理由、同じ目的である根拠を書いてください。昔の許可文書や無関係な資料を流用しません。ファイル名やhashの一致だけでは内容の正しさを証明しないため、内容は編集者が確認します。選択ボタンに重ねると説明も出ます。

編集者の既定表示はOwner、記録上のIDは `actor:local-owner`。本人認証ではなく、ローカル操作者の申告です。`permit`も本人が実際に許可した内容を記録する操作であり、認証の関門ではありません。`--actor`を変えただけで、既存の編集等の許可はそのIDへ移りません。

Mapの接続一覧はこのフォルダの `data/viewer-session.json` にだけ保存します。他のアプリや開発環境の一覧、Codexのチャット一覧は読みません。「Mapを閉じる」は接続一覧から外す操作で、正本を削除しません。GUIは他のローカルMapも明示して開けますが、それぞれの許可を流用しません。

使用済みフォルダを移動・複製した時は、同じprojectの旧保存先を自動で開かず、このフォルダのMapを優先します。独立した他Mapの一覧は保ちます。複製を元と並行して第二の正本として更新せず、保存先を確認してください。

## 仕事を登録する

```powershell
python workmap.py add --parent W0 --name "月次報告を整える" --intent "報告を受ける人が状況と必要な判断を理解できるようにする" --origin "月次報告の担当になった" --owner-instruction "この仕事をW0の子として登録する"
python workmap.py show
python workmap.py show --work W1
python workmap.py thread --work W1
```

`--parent` は現在Mapで確認した住所を指定します。内部IDと空き住所、revision、競合検査、保存、読戻しは既存入口が扱います。登録だけでは担当Pinや実作業Permissionを追加しません。

「後で行う」と決めた仕事も今登録します。後でやめるなら理由を残して閉じます。Intent／Originを意味Threadに重複させません。出生時にそれだけでは残せない重要な理由がある場合だけ `add --reason "理由"` を付けられます。

Work追加は、引受や編集権限の自動付与とは別です。新しいW1を自分で扱うなら、必要な操作だけ一度明示します。Map全体への追加許可は、編集・終了等の許可を子へ継承しません。今回の明示的なOwner登録指示で一件を追加する場合も、既知の有効なMap追加禁止は無視しません。

```powershell
python workmap.py permit --work W1 --edit --record --pin --close --owner-instruction "W1の訂正、理由と結果の記録、本人Pin操作、通常契約を満たす場合の終了をOwnerに許可する"
```

Map全体への追加許可はproject・一つのroot・DBの絶対パスに結び付きます。配布フォルダを移した場合や別Mapへコピーした場合は、そのまま有効とはせず、現在の場所と意図を確認して許可を設定し直します。

## 現在地と理由を読む

- `show`：現在の仕事の木。
- `show --work W1`：目的、親、関与、許可、制約、依存、理由など。
- `thread` または `thread --work W1`：将来の判断に必要な意味だけ。
- `history --work W1`：操作の技術履歴を明示的に確認するとき。

通常の登録直後にThreadが空でも異常ではありません。出生時の理由はWorkのIntentとOriginにあります。unknownや読取不能を、空集合・未実行・成功と決めつけません。

AIへ必要な現在情報だけ渡す時は `python workmap.py show --work W1 --brief` を使えます。Workの目的・Parent目的・担当・許可・行動ルールと理由／結果の索引を返します。表示成功を実行許可や仕事完了の証明にはしません。

その続きは、必要なものだけを次のコマンドで読みます。

```powershell
python workmap.py show --work W1
python workmap.py thread --work W1
python workmap.py history --work W1
```

順にWorkの詳細、意味Thread、技術履歴です。三つを毎回読む必要はありません。これらは次の読取時点の版を取得するので、先のbriefと同じrevisionであることを強制しません。判断や更新の前には現在の目的・許可・行動ルール等を確認します。

返答の`retrieve`や省略説明にある`--entry`、`--evidence`、`--expected-revision`は、下位の`tools.read_workmap_store`用です。`workmap.py`へは渡せません。同じrevisionの索引から一件だけ読む必要がある場合は、配布フォルダで次を使います。

```powershell
$env:PYTHONPATH = "$PWD;$PWD/src"
python -X utf8 -m tools.read_workmap_store --backend sqlite --store "$PWD/data/workmap.sqlite3" --expected-project "実際のproject_id" --evidence-repo "$PWD" --work W1 --thread --entry "索引の理由ID" --expected-revision "briefのrevision"
```

`project_id`と`revision`はこのMapのshow／briefの返答から、IDはその索引から転記します。開発用project IDを流用しません。結果なら`--thread`を`--evidence`へ、IDを結果IDへ替えます。版が変わっていれば止まるため、現在情報を読み直して再判断します。Pin出典等を同じ版の詳細から読む時は`--thread --entry …`を外し、`--brief`を付けません。この取得は許可やunknownの検査を省略する手段ではありません。

## 引き受ける・理由を残す・終了する

```powershell
python workmap.py pin --work W1 --reason "月次報告の確認を主作業として引き受ける" --owner-instruction "本人の主作業をW1にする"
python workmap.py reason --work W1 --text "受け手の判断に必要な内容を先に確認し、装飾は後で扱う" --owner-instruction "この判断理由を残す"
```

別のWorkへ切り替える時も同じ `pin` です。最初の引受は対象のPin許可と、本人全体・祖先を含む関連前提を確認して通常CoreのPin追加を行います。既存activeからの切替・対象heldへの復帰では、今回のOwner指示を根拠に、足りない操作だけの一時許可を保存し、切替後に外します。常設の`permit --pin`は、この通常切替の毎回の必須手順ではありません。別Workのheldだけがあり、新しい対象を引き受ける時は初回の経路です。返信待ちだけでheldにしません。関連する行動ルール・依存・unknownがある場合は、実際の根拠を確認してbasisオプションを指定します。[Pinの範囲と回復](docs/work-management/PIN_ROUTINE_ENTRY_2026-10-02.md)。

`reason`は将来の判断に必要な「なぜ」です。試験・保存・成果の記録は、実際の根拠ファイルを作って `result` で接続します。最新の結果だけで別の残務を消しません。

```powershell
python workmap.py result --work W1 --text "報告内容の確認結果" --source repo:docs/work-management/evidence/report.txt
python workmap.py close --work W1 --outcome completed --reason "元の目的を達成し残務を処遇した理由" --evidence repo:docs/work-management/evidence/report.txt --release-own-pin --owner-instruction "根拠と現在状態を確認してW1をcompletedで閉じ、本人Pinを解除する"
```

`report.txt`は例です。実際の成果と判断を確認できるファイルへ置き換えます。completedにはIntent達成の根拠が必要です。取りやめは `stopped`、置換は `replaced` と `--successor` を使い、未実施を完了扱いにしません。子・他担当・依存・行動ルール・判断を変えるunknown等が残る場合は拒否されます。close後の名前・最終Intent・出自・終了理由はThreadから辿れます。親への復帰は自動ではなく、明示した存続Workへ `pin` で戻ります。

## 応答が分からなくなったとき

新しい登録をやり直す前に `show` で現在Mapを確認します。CLI要求は `data/transition-requests/` に保存されます。操作に対応する入口へ、返された**その要求ファイル**を渡します。

```powershell
# CLIのinit／addで保存された追加要求
python workmap.py resume-add --request "data/transition-requests/実際の追加要求ファイル名.json"

# Pin切替・理由／結果記録・許可保存・close等の対応するCLI要求
python workmap.py resume --request "data/transition-requests/実際の要求ファイル名.json"
```

追加要求は`resume-add`を使います。その他の対応するCLI要求を`resume`で確認します。適用済みなら履歴を確認し、その後の変更を巻き戻しません。未適用で版・根拠が変わっていれば止まるので、元要求を書き換えず再判断します。

GUIの訂正・追加で保存された要求は、このCLIへ流用しません。GUIの結果確認と現在Mapを照合し、なお不明なら元要求と根拠を保全して判断を引き継ぎます。GUI要求まで一つのコマンドで再確認できるという保証ではありません。

確認できない場合はunknownのまま、Map・履歴・Owner指示を照合します。DBや履歴を手編集して成功扱いにしません。初期化が空DB作成直後に中断し、要求ファイルがまだない場合は、元のdataとdocsを保全・退避し、新しいフォルダでinitから始め直します。既存データを削除してやり直す案内ではありません。

## バックアップと復旧確認

新しい保存先フォルダを作って実行します。

```powershell
New-Item -ItemType Directory C:\WorkMapBackups\day1
python workmap.py backup --output C:\WorkMapBackups\day1\backup.json
```

例の保存先は配布フォルダの外です。可能なら別媒体へも保管してください。出力されたSHA-256と、同じ場所へ自動保存されたThread選別資料も一緒に保管します。**別途、この試用版のソースと `docs`、`data/transition-requests` も保全してください。** DBのバックアップには、リンク先の社内文書や外部Artifactの実体は含まれません。バックアップは編集する第二正本ではありません。

```powershell
python workmap.py recover-check --backup C:\WorkMapBackups\day1\backup.json --sha256 "出力されたSHA-256" --target recovery-check/candidate.sqlite3
python workmap.py recovery-show --candidate recovery-check/candidate.sqlite3 --work W0 --thread
```

`recovery-show`は候補専用の読取であり、現在の正本として扱いません。`--work`を必要なWorkへ替えて、目的・担当・許可・行動ルール・理由と未確認を確認します。終了Workの理由も`--thread`から回収できます。

通常利用へ戻す時は、すべてのGUI・書込処理を閉じます。元のDBとdocsを別の場所へ保全し、`data/workmap.sqlite3`を退避してください。`-journal`等が残っていれば、それも元DBと一緒に保全・退避します。下の操作は、DBや付随ファイルがまだある保存先には書きません。

```powershell
python workmap.py restore --backup C:\WorkMapBackups\day1\backup.json --sha256 "元backupのSHA-256" --candidate recovery-check/candidate.sqlite3 --candidate-token "recovery-showのcandidate_token" --project-id "同じ表示のproject_id" --owner-instruction "候補の目的・現在値・理由と未確認を確認し、元を保全したうえでこのMapとして復旧する"
python workmap.py show
```

選んだ候補と元backupが一致することを再確認して、空いている保存先へだけ復旧します。新しいrevisionを付けるため、復旧前の未適用要求を古い前提で黙って適用しません。保存先を移した場合のMap追加許可は、自動で結び直さず、必要なら新しい場所を確認して`permit --map-add`から設定し直します。

復旧は選んだbackup時点の回復です。その後に失われた更新、外部文書の実体・有効性、現在の許可は別に照合してください。不明は成功・未実行と決めつけません。復旧結果が不明なら、返された`.restore.json`を同じ`resume`で確認できます。

## 仕組みを育てる人へ

[HANDOVER.md](HANDOVER.md)に設計の要点・ファイル配置・残る範囲があります。GUIと上記の日常操作は既存Coreへ接続済みです。reopen、関係変更、複雑な残務処遇など、この薄い入口がまだ扱わない操作は、既存Coreの契約と根拠を確認して接続してください。複数PC共有、自動マージ、現用正本への自動復旧切替はこの候補の提供範囲ではありません。

この配布は個人向けローカル試用版です。会社で使う場合の実起動・利用条件は会社側で確認してください。全社導入の適合確認はしていません。利用後に作られる実Work・根拠・バックアップを公開リポジトリへコミットしないでください。

同梱の確認は `python -X utf8 check.py`、またはPowerShellの `tools/run-tests.ps1` で行えます。使い捨ての写しでGUI起動と操作を確認し、利用者のdata内の正本は開きません。旧0.4.1のDBをそのまま使うための移行確認は含めません。旧版へ上書き展開せず、新しいフォルダで試してください。

## ライセンス

コード等は[MIT](LICENSE)、説明文書は[CC BY 4.0](LICENSE-DOCS.md)です。適用範囲と著作者表示は[NOTICE](NOTICE.md)、初期版の到達範囲は[リリースノート](RELEASE_NOTES.md)を参照してください。
