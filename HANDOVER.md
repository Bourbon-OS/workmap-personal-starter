# 会社側への開発引渡し

## 守る目的

Work Mapは現在の仕事全体と関係・現在地から次を判断するもの。Work Threadは現在値だけでは分からない過去の「なぜ」を判断へ戻すもの。記録を増やすことを目的にしない。

- WorkはName、Intent、Origin、不変のwork_keyを持つ。表示IDは住所。
- Parentは現在の包含、Dependencyは必要関係、Originは出自。混同しない。
- Pinは主体の関与、Permissionは許可、Conditionは判断に効く制約。登録は実行許可ではない。
- 「後でやる」と決めた仕事もMapへ登録する。見送るときは理由を残して通常のcloseで処遇する。
- Threadは生存中から育つ。全操作を通常Threadに表示しない。技術履歴は保持する。
- close後は現在Mapから退出する。最終の識別情報・目的・出自・終了理由・必要な知見・再開条件・根拠へThreadから戻れること。別の退役Work集合を追加しない。
- 目的そのものが変わるなら新Work。同じ目的の訂正・明確化は根拠と変更理由を保持する。
- unknownを推測で埋めない。操作の判断へ影響するunknownは止める。
- Storeは現在値の正本を一つにする。生成画面、バックアップ、AIの要約を別の編集正本にしない。

## 構成

| 場所 | 役割 |
|---|---|
| workmap.py | 個人用の薄い起動入口。既存Coreへ接続 |
| src/pma/workmap_contract.py | データ・現在文脈の検査 |
| src/pma/workmap_canonical_core.py | Workや関係、Pin、許可、close等の更新契約 |
| src/pma/workmap_thread_view.py | 意味Threadと技術履歴の表示分離 |
| src/pma/workmap_sqlite_store.py | 同一Windows host・同期外local向けSQLite rollback保存 |
| tools/ | 登録、読取り、レビュー済み要求適用、バックアップ、復旧候補確認 |
| data/workmap.sqlite3 | 初期化後に生成される唯一の現在正本 |
| docs/work-management/evidence/ | 初期化後のOwner登録指示や選別根拠 |
| check.py | 個人用入口を使い捨ての写しで確認する合成試験 |

CoreとStore adapterの境界は維持する。現在のSQLite採用を将来の全環境へ固定しない。共有ドライブやクラウド同期へDBを置くだけでチーム利用成立とはしない。

## 最初に伸ばす部分

1. 実際の仕事を少数登録し、目的と現在地を読めるか確認する。
2. よく行う操作から、既存Coreを呼ぶ入口を整える。Pin・許可・制約・競合の検査は省略しない。
3. 同じ正本を読む人間向け画面を作る。画面内に独自のWork現在値を持たせない。
4. 中断・担当交代で理由を回収できるか確認する。

拡大・部分表示・別Map統合は、同一性・権限・参照・当時の理由を保つ設計が必要。今回は独立したMap同士の双方向同期・自動マージを提供しない。

## 再確認

Python 3.11以上、標準ライブラリのみ。配布フォルダで次を実行する。

```powershell
python -X utf8 check.py
```

合成試験の成功は、会社の実環境・長期運用・利用規程との適合の証明ではない。閉じる判断やOwnerの帰属確認は、hashやフラグだけでは自動証明できない。
