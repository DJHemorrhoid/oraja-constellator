# oraja-constellator

**Connect the charts. Make the stars align.**

oraja-constellatorは、さまざまな難易度表に広がる譜面を、傾向・密度・プレイ状況などの条件でまとめ、目的に合ったカスタムフォルダーを構成するツールです。

beatoraja専用です。

## 主な機能

- 難易度表とフィルターを選んでカスタムフォルダーを生成
- 譜面傾向・平均密度・プレイ状況・クリア状況・スコア状況による絞り込み
- 各フィルター結果からランダムコースを生成
- プレイ中に経過日数・最小BP・スコア率を変更できる小型パネル

## 必要なもの

- Windows
- Python 3.11以上
- beatoraja

外部Pythonパッケージの追加インストールは不要です。

## 導入

1. Releasesから `oraja-constellator_v1.0.17_public.zip` をダウンロードします。
2. ZIPを任意のフォルダーへ展開します。
3. `start_oraja_constellator.bat` を実行します。
4. 画面の案内に従ってbeatoraja環境を読み込み、差分解析とカスタムフォルダーへの反映を行います。

詳しい操作は配布ZIP内の `README.txt` を参照してください。

## songdata.dbへの書き込み

カスタムフォルダーを利用できるようにするため、beatorajaの `songdata.db` へ本ツール専用テーブルを追加・更新します。標準の楽曲情報やプレイ記録には書き込みません。

初回の書き込み前に `songdata.db` を1度だけ自動バックアップします。保存先はツール内の `data\backups\songdata` です。

## License

MIT License
