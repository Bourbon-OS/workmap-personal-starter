# 開発・拡張を引き継ぐ人へ

## 守る目的

作られた理由、対象者、Workが扱う広い範囲と一続きの利用例は[README](README.md)、利用時の操作手順は[USAGE](USAGE.md)から読む。本書は、その目的を守って仕組みを育てる人への開発引渡しである。

目的や判断の根拠となる足場を、開発ではAshibaと呼んでいる。仕事が生まれた時からその足場を保ち、次の判断へ渡すという動機は維持する。公開READMEでは、この開発上の呼称を覚えることを利用の前提にしない。

Work Mapは現在存在する目的を持つ対象と関係・現在地から次を判断するもの。Work Threadは現在値だけでは分からない過去の「なぜ」を判断へ戻すもの。継続するサービス・活動・検討中の案と小作業を同じWorkで捉え、目先の操作だけで親目的を置き換えない。記録を増やすことを目的にしない。

親Intentは新しい手段・Workを生むためにも使う。「より多くの読者に届ける」目的から電子版を試すWorkを考えられるように、現在の手段へ親目的を固定しない。選ぶのは人・担当AIであり、登録・採用・引受・実行許可を分ける。AIが読み書きする前提の構造化された現在情報・段階的な読取・通常入口と、unknown・出所・版・許可・競合の検査を維持する。人間も同じ言葉・正本をGUI等から扱え、AIなしでも利用できる。この説明を自動の目的達成保証へ広げない。

- WorkはName、Intent、Origin、不変のwork_keyを持つ。表示IDは住所。
- Parentは現在の包含、Dependencyは必要関係、Originは出自。混同しない。
- Pinは主体の関与、Permissionは許可、Action Rule／行動ルールは特定のWork・主体・行動に効く局所ルール。登録は実行許可ではない。
- 「後でやる」と決めた仕事もMapへ登録する。見送るときは理由を残して通常のcloseで処遇する。
- Threadは生存中から育つ。全操作を通常Threadに表示しない。技術履歴は保持する。
- close後は現在Mapから退出する。最終の識別情報・目的・出自・終了理由・必要な知見・再開条件・根拠へThreadから戻れること。別の退役Work集合を追加しない。
- 目的そのものが変わるなら新Work。同じ目的の訂正・明確化は根拠と変更理由を保持する。
- unknownを推測で埋めない。操作の判断へ影響するunknownは止める。
- Storeは現在値の正本を一つにする。生成画面、バックアップ、AIの要約を別の編集正本にしない。

## 構成

公開版v0.2.5と、保存データの形式番号0.4.2は別のものです。利用者向けの案内では公開版を使い、保存形式を扱う実装・互換性の判断でだけ形式番号を使います。旧0.4.1形式のデータ移行は未確認です。

| 場所 | 役割 |
|---|---|
| workmap.py | 個人用の薄い起動入口。既存Coreへ接続 |
| WorkMap.pyw | 同梱GUIの起動。開発repo・Codex・運用Stewardに依存しない |
| src/pma/workmap_contract.py | データ・現在文脈の検査 |
| src/pma/workmap_canonical_core.py | Workや関係、Pin、許可、close等の更新契約 |
| src/pma/workmap_thread_view.py | 意味Threadと技術履歴の表示分離 |
| src/pma/workmap_sqlite_store.py | 同一Windows host・同期外local向けSQLite rollback保存 |
| tools/ | 登録、読取り、本人Pin切替、結果記録、close、バックアップ、候補確認と明示復旧 |
| data/workmap.sqlite3 | 初期化後に生成される唯一の現在正本 |
| docs/work-management/evidence/ | 初期化後のOwner登録指示や選別根拠 |
| check.py | 個人用入口を使い捨ての写しで確認する合成試験 |

GUIの表示・編集・Work追加・自動更新は既存実装を再利用する。Mapの中身をUI設定へ複製せず、接続一覧だけをdata/viewer-session.jsonへ保存する。チャット名の外部照合は既定で使わない。Ownerの既定IDはactor:local-owner、表示名はOwnerで、認証の代用ではない。

permitは既存set_permissionの薄い入口であり新しい権限モデルではない。Work単位の編集・記録・Pin・closeを明示し、Map全体の新規追加だけは既存add_work_in_mapのproject・root・絶対パス束縛を用いる。対象操作に必要な許可やunknownの扱いを省略しない。

明示されたOwner登録指示による一件の追加でも、既知の有効なMap追加禁止は解除されない。USAGEの平易な説明でこの検査の例外を認めたとは扱わない。

通常のPin切替は、今回のOwner指示から必要な一時Permissionを保存・使用・終了する既存経路も使う。常設permitを毎回要求するものではない。初回も関連する現在文脈を確認し、Pinの引受から本作業の許可を推測しない。

既存activeからの切替と対象heldへの復帰では、必要な一時許可を既存経路で扱う。activeがなく、別のheldから新しい対象を引き受ける場合は初回経路になる。対象Workを初めて選ぶかだけで一律にpermitの要否を分けない。

編集者の既定IDは`actor:local-owner`。`--actor`だけを変更しても、そのIDに既存許可は移らない。CLIの`owner_source`は本人の実際の指示を文書として保存する機能で、本人認証や、AIが指示を捏造できないという保証ではない。

接続一覧は配布フォルダの`data/viewer-session.json`へ保存する。開発環境やCodexの一覧は取得しない。フォルダの移動・複製では、同じprojectの旧保存先を自動接続せず、当該フォルダの正本を優先する。独立した別Mapの一覧を保持する。複製を二つの更新正本として並行利用しない。

GUIのWork追加は現在、`docs`内の根拠ファイルを必須とする。CLIのaddは登録指示からその文書を作るため、人間の通常追加には入力負担の差がある。画面内の本人入力と反映確認から既存の根拠記録を作る案は、次の実装検討である。今回の文書修正で、この機能が実装済みになったとは扱わない。

保存済み要求は、CLIの`init`／`add`なら`resume-add --request`、Pin・理由・結果・許可・close等の対応するCLI要求なら`resume --request`から既存の回収処理へ渡す。[応答不明時の手順](USAGE.md#応答が分からなくなったとき)で形式と操作を区別し、GUI要求をこのCLIへ流用しない。復旧は`recover-check`、候補専用読取`recovery-show`、元データ保全後の`restore`で行う。既存DBやSQLite付随ファイルには上書きせず、選んだbackup・候補・projectと根拠を再確認して新revisionで再開する。候補やバックアップを第二の編集正本にしない。外部Artifactの実体や失われた後続更新の完全回復は保証しない。

CoreとStore adapterの境界は維持する。現在のSQLite採用を将来の全環境へ固定しない。共有ドライブやクラウド同期へDBを置くだけでチーム利用成立とはしない。

## 同じ版の記録を絞り込んで読む

通常の閲覧はUSAGEの画面とコマンドを使う。AIや独自連携が、先に得た索引と同じ版から本文一件を追加取得する時だけ、以下を使う。

返答の`retrieve`や省略説明にある`--entry`、`--evidence`、`--expected-revision`は、`tools.read_workmap_store`用で、`workmap.py`の引数ではない。配布フォルダから実行する。

```powershell
$env:PYTHONPATH = "$PWD;$PWD/src"
python -X utf8 -m tools.read_workmap_store --backend sqlite --store "$PWD/data/workmap.sqlite3" --expected-project "実際のproject_id" --evidence-repo "$PWD" --work W1 --thread --entry "索引の理由ID" --expected-revision "briefのrevision"
```

`project_id`と`revision`はこのMapのshow／briefの返答から、IDはその索引から取る。開発projectのIDを流用しない。結果なら`--thread`を`--evidence`へ、IDを結果IDへ替える。版が違えば現在情報から読み直して再判断する。Pin出典など同じ版の詳細を読む時は`--thread --entry …`を外し、`--brief`を付けない。この取得でも許可・unknownの検査は維持する。

## 最初に伸ばす部分

1. 実際の仕事を少数登録し、目的と現在地を読めるか確認する。
2. 同梱のGUI・日常コマンドで足りない実例があれば、具体的な目的を持つWorkとして検討する。Pin・許可・行動ルール・競合の検査は省略しない。
3. 画面に独自のWork現在値や共有自由文進捗を作らず、既存正本と必要なThread／Evidenceから判断する。
4. 中断・担当交代で理由を回収できるか確認する。

拡大・部分表示・別Map統合は、同一性・権限・参照・当時の理由を保つ設計が必要。今回は独立したMap同士の双方向同期・自動マージを提供しない。

## 新しい担当へ渡す時

初回はREADMEの考え方と利用例へ案内し、必要な操作だけUSAGEから辿る。開発を引き継ぐ人には本書も渡す。利用先のDBの絶対パス・project identity・本人actor・許可された範囲を渡し、実際のproject identityはshowの返答から読む。開発プロジェクトのIDやStoreを流用しない。必要な現在Mapから読み、全履歴や全設計を毎回必読にしない。

予定した確認が一まとまり終わった時と、予定外の仕組み・準備で範囲や方法を広げる前に、次が親目的に本当に必要かを結果から判断する。毎操作の作文やOwner確認を増やさない。独立して検討したいことは、採否・着手と分けて短いIntent/OriginでWorkに残せる。将来判断に必要なWhyはThread、結果と成果物の所在はEvidenceへ置き、同じ内容を重複させない。

## 再確認

Python 3.11以上、標準ライブラリのみ。配布フォルダで次を実行する。

```powershell
python -X utf8 check.py
```

合成試験の成功は、会社の実環境・長期運用・利用規程との適合の証明ではない。閉じる判断やOwnerの帰属確認は、hashやフラグだけでは自動証明できない。
