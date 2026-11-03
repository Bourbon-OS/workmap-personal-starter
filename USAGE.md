# Work Map／Work Threadを使い始める

考え方と一続きの利用例は[README](README.md)にあります。この資料は、初期設定と必要な操作を調べるための手順です。毎回全文を読む必要はありません。

## 最初にすること

1. ZIPを利用するWindows PCの**同期対象外のローカルフォルダ**へ展開します。例：`C:\WorkMapPersonal`。展開された`workmap.py`と`README.md`のあるフォルダを使います。OneDrive、共有フォルダ、ネットワークドライブ上では運用しません。
2. エクスプローラーでそのフォルダを開き、上部のアドレスバーに`powershell`と入力してEnterを押します。開いたPowerShellで`python --version`を実行し、**Python 3.11以上**と表示されるか確認します。
   Pythonが見つからない、または3.11未満の場合は、[Python公式ダウンロードページ](https://www.python.org/downloads/)からWindows用を入手してインストールし、PowerShellを開き直してもう一度確認します。詳しい手順は[公式のWindows導入案内](https://docs.python.org/3/using/windows.html)にあります。
   続けて`python -m tkinter`を実行し、小さなウィンドウが開けば画面表示の準備もできています。確認したらそのウィンドウを閉じます。追加ライブラリは不要です。
   ウィンドウが開かない場合は、表示されたエラー文を控えます。`tkinter`や`_tkinter`が見つからない場合は、利用中のPythonの提供元で、画面表示用のTcl/Tkの追加・修復方法を確認してください（[Tkinter公式案内](https://docs.python.org/3/library/tkinter.html)）。`python`コマンド自体が動かない場合は、[Windows版のトラブルシューティング](https://docs.python.org/3/using/windows.html#troubleshooting)を参照してください。
3. 次の例を自分の仕事の言葉へ直して実行します。本人の登録指示として扱われるので、AIに実行させる場合も実際に承認した内容を渡してください。

```powershell
python workmap.py init --name "自分の仕事" --intent "担当業務の目的と判断理由を保ち、確実に引き継げるようにする" --origin "自分の仕事でWork Mapを使い始める" --owner-instruction "この名前・目的・由来で、自分の仕事の親Workを登録する"
python workmap.py gui
```

最初のWorkはW0です。名前・目的・生まれたきっかけは、自分の仕事に合わせて決めてください。`--owner-instruction`には、この内容で登録すると決めたことを書きます。初期化の再実行で既存のデータが上書きされることはありません。

ここまででMapを眺められます。追加・編集の許可設定は、実際にその操作を使う時に行います。

## GUIで開く

初期化した後は、`WorkMap.pyw`をダブルクリックするか、次のコマンドで開きます。

```powershell
python workmap.py gui
```

左の木からWorkを選ぶと、詳細と判断理由を読めます。担当別表示や、自動更新のON／OFFも画面から使えます。「Mapを閉じる」は表示対象から外す操作で、データは残ります。

編集者の初期表示はOwnerです。自分以外の担当が使う時は、その担当の名前と許可を確かめてください。Pin操作やWorkの終了は、後述のコマンドで行います。

## 仕事を登録する

```powershell
python workmap.py add --parent W0 --name "月次報告を整える" --intent "報告を受ける人が状況と必要な判断を理解できるようにする" --origin "月次報告の担当になった" --owner-instruction "この仕事をW0の子として登録する"
```

画面を開いたままなら、画面へ戻って**F5で再読込**します。W0の下に、追加したW1が表示されます。自動更新をONにしている場合は、その更新でも反映されます。

`--parent`には、Mapで確認した親Workの番号を指定します。登録内容を保存し、読み戻した結果が表示されます。登録しただけでは、その仕事を引き受けたり、実行を許可したことにはなりません。

「後で行う」と決めた仕事も今登録します。後でやめるなら理由を残して閉じます。目的ときっかけはWorkに残るので、Threadに書き直す必要はありません。それだけでは残せない重要な理由がある場合だけ `add --reason "理由"` を付けられます。

### 登録したWorkの操作を許可する

新しいW1を自分で扱う時は、そのWorkで使う操作を別に選びます。Workを登録しても、編集や終了まで許可されたことにはなりません。次は訂正・記録・Pin操作・終了をすべて使う場合の例です。使う操作だけを指定してください。

```powershell
python workmap.py permit --work W1 --edit --record --pin --close --owner-instruction "W1の訂正、理由と結果の記録、本人Pin操作、終了に必要な確認を済ませた場合の終了をOwnerに許可する"
```

### 画面から追加・訂正する準備

自分のMapでも、「誰が操作するか」と「何をしてよいか」は分けて記録します。人とAIが同じMapを使った時に、担当になったことを、何でも変更してよいという許可と取り違えないためです。一人で使う場合は、自分で必要な操作を選びます。

これは操作範囲の記録で、ログインや本人認証ではありません。AIに操作を任せる場合は、本人が決めた範囲だけを伝えます。

画面から新しいWorkを追加する時は、一度だけ、このMapへの追加を許可します。

```powershell
python workmap.py permit --work W0 --map-add --owner-instruction "このMapの現在・今後のWorkの下への新しいWork追加をOwnerに許可する"
```

配布フォルダを移した場合や別Mapへコピーした場合は、現在の保存先を確認し、Mapへの追加許可を設定し直してください。

今のGUIでは、追加内容の根拠になる文書も必要です。`docs`配下に、登録する名前・目的・生まれたきっかけを本人が採用したことを書いたテキストを保存し、「資料を選ぶ」から指定します。手元に文書を用意せず追加したい時は、上の「仕事を登録する」のコマンドを使えます。入力した登録指示から、必要な記録を作ります。

既存Workの名前・目的・出自を画面で訂正する時は、対象Workへの編集を許可します。

```powershell
python workmap.py permit --work W0 --edit --owner-instruction "W0の名前・目的・出自の訂正をOwnerに許可する"
```

目的の訂正・明確化には、`docs/work-management/evidence/owner_任意名.txt`へ本人の判断、変更前後、理由、同じ目的である根拠を書いて選択します。追加・訂正のどちらも、反映前に表示される対象と内容を確認してから保存します。ここで設定した編集許可が、別のWorkへ自動で広がることはありません。

## 現在地と理由を読む

- `show`：現在の仕事の木。
- `show --work W1`：目的、親、担当、許可、行動ルール、依存、理由など。
- `thread` または `thread --work W1`：将来の判断に必要な意味だけ。
- `history --work W1`：操作の技術履歴を明示的に確認するとき。

通常の登録直後にThreadが空でも異常ではありません。生まれた理由はWorkの目的ときっかけにあります。未確認の項目は、確認した結果「何もない」場合と区別して扱います。

AIへ必要な現在情報だけ渡す時は `python workmap.py show --work W1 --brief` を使えます。Workの目的・親の目的・担当・許可・行動ルールと理由／結果の索引を返します。

その続きは、必要なものだけを次のコマンドで読みます。

```powershell
python workmap.py show --work W1
python workmap.py thread --work W1
python workmap.py history --work W1
```

順にWorkの詳細、Work Thread、技術履歴です。三つを毎回読む必要はありません。各コマンドは、実行した時点の最新を読みます。複数の結果を組み合わせて判断する時は、同じ版か確認します。判断や更新の前には、現在の目的・許可・行動ルール等を確認します。

AIや独自の連携から、同じ版の記録一件だけを追加で読みたい場合は、[絞り込んで読む方法](HANDOVER.md#同じ版の記録を絞り込んで読む)を使います。通常の閲覧は、上の画面やコマンドで始められます。

## 引き受ける・理由を残す・終了する

```powershell
python workmap.py pin --work W1 --reason "月次報告の確認を主作業として引き受ける" --owner-instruction "本人の主作業をW1にする"
python workmap.py reason --work W1 --text "受け手の判断に必要な内容を先に確認し、装飾は後で扱う" --owner-instruction "この判断理由を残す"
```

別のWorkへ切り替える時も、同じ`pin`を使います。初めて担当を引き受ける時は、そのWorkのPin操作を`permit --pin`で許可します。現在の主作業からの切替や、保持しているWorkへ戻る時は、毎回の`permit`は要りません。主作業がなく、保持中のものとは別の新しいWorkを引き受ける場合も、対象の許可が必要です。

返信待ちだけでheldへ変える必要はありません。行動ルールや依存関係、未確認事項で止まった時は、その内容と根拠を確かめてください。[Pinの詳しい条件と回復](docs/work-management/PIN_ROUTINE_ENTRY_2026-10-02.md)に、状況別の操作があります。

`reason`は将来の判断に必要な「なぜ」です。試験・保存・成果の記録は、実際の根拠ファイルを作って `result` で接続します。結果を記録しても、ほかの残っている仕事が終わったことにはなりません。

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

結果を確認できない時は、実行済み・未実行を決めつけず、Map・履歴・本人の指示を照合します。DBや履歴を手編集して成功扱いにしません。初期化が空DB作成直後に中断し、要求ファイルがまだない場合は、元のdataとdocsを保全・退避し、新しいフォルダでinitから始め直します。既存データを削除してやり直す案内ではありません。

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

[HANDOVER.md](HANDOVER.md)に設計・プログラムの配置・拡張時の確認事項があります。終了したWorkを戻す操作、関係変更、複雑な残務の処遇など、ここで案内していない操作を追加する場合は、そちらを参照してください。

この配布は個人向けローカル試用版です。会社で使う場合の実起動・利用条件は会社側で確認してください。全社導入の適合確認はしていません。利用後に作られる実Work・根拠・バックアップを公開リポジトリへコミットしないでください。

動作確認は `python -X utf8 check.py`で行えます。使い捨ての写しで画面の起動と操作を確認するため、利用中のMapには触れません。以前のデータの移行は別に確認が必要です。新しいフォルダから試してください。

## ライセンス

コード等は[MIT](LICENSE)、説明文書は[CC BY 4.0](LICENSE-DOCS.md)です。適用範囲と著作者表示は[NOTICE](NOTICE.md)、初期版の到達範囲は[リリースノート](RELEASE_NOTES.md)を参照してください。
