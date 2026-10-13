# MCP server for Fujifilm Digital Camera control (fujixcam-mcp)

## 概要
PCに接続されたFUJIFILMデジタルカメラの撮影制御を提供するMCPサーバーです。\
MCP server that provides capture control for FUJIFILM digital cameras connected to a PC.
- Webカメラでは撮れない撮影領域の映像をAIにつなげて新しい用途を開拓します。
- 本ソフトウエアはオープンソースで提供されており、GitHub から入手可能です。
  - https://github.com/mterada/fujixcam-mcp
- 本ソフトウエアの動作には２つの外部モジュールが必要です。
  - FUJIFILM Digital Camera SDK（メーカーのサイトから入手可能）
  - fujixcam (上記SDKのPythonラッパー、PyPIから入手可能)
  後者は本ソフトウエアのinstall時に自動的にインストールされますが、前者のSDKはユーザー自身でインストールする必要があります。
  ```bash
  python -c "import fujixcam;fujixcam.install_sdk()"
  ```
  詳細はこちらをご参照ください -> https://github.com/mterada/fujixcam-api#readme
- ツールの説明は、以下のセクションを参照してください。

## 機能

- **カメラ接続管理**: カメラデバイスへの接続・切断
- **設定値の取得**: 現在のカメラ設定と接続状態の確認
- **オプション取得**: 撮影モード（PSAM）、露出補正、絞り値、シャッター速度、ISO感度などのオプションの確認
- **設定値の変更**: 撮影モード（PSAM）切替え、露出補正・絞り値・シャッター速度・ISO感度設定などの調整
- **フォーカス制御**: MF/AF-S/AF-C の切り替えと AFエリアモードの調整
- **視野確認**: カメラのファインダーに表示されているライブビュー画像の取得と試し撮りの実行
- **写真撮影**: 通常撮影およびバルブ撮影を実行し、ローカルフォルダに保存

## 必要要件

- Python 3.11以上
- MCP Python SDK 2.x (`mcp[cli]>=2,<3`)
- Windows 11 or macOS 14+
- デジタルカメラ FUJIFILM X/GFX シリーズ（USB Tethering mode）

## プログラムの構成

- サーバー本体：	src/fujixcam_mcp/server.py

## 使用方法

### インストール

MCPサーバー `fujixcam-mcp` のインストール
```bash
pip install fujixcam-mcp
# 'fujixcam' will be installed automatically as a dependency

```
下位レイヤーの FUJIFILM Digital Camera SDK のインストール
```bash
python -c "import fujixcam;fujixcam.install_sdk()"
```

### スタンドアロン実行

```bash
cd src
python -m fujixcam_mcp.server
```

### AI クライアントとの統合

`mcp.json` ファイルを含んでいます。

```json
{
  "servers": {
    "fujixcam-mcp": {
      "command": "python",
      "args": [
        "-m",
        "fujixcam_mcp.server"
      ],
    }
  }
}

```

## 利用可能なツール

すべてのtoolは統一されたJSON形式を返します。

```json
{
  "ok": true,
  "code": "OK",
  "message": "...",
  "data": { },
  "error": null,
  "meta": {
    "tool": "tool_name",
    "timestamp": "2026-09-25T00:00:00+00:00"
  }
}
```
message には可読なエラーメッセージ文が入ります。

失敗時は `ok=false` で、`code` に機械可読なエラーコードが入ります。
- `CAMERA_NOT_CONNECTED`
- `INVALID_ARGUMENT`
- `SDK_CALL_FAILED`
- `CAPTURE_FAILED`
error には hint などの追加情報が入ります。

```json
{
  "ok": false,
  "code": "CAMERA_NOT_CONNECTED",
  "message": "Camera is not connected",
  "data": null,
  "error": {
    "retryable": true,
    "hint": "camera power on and reconnect"
  }
}
```

### 接続管理

#### connect_camera(camera_id: str = "")
カメラとの通信接続を確立します。
カメラの電源がOFFになって接続が切れた場合は、電源をONにした後、再度 `connect_camera()` を呼び出して接続を確立してください。
FUJIFILM Digital Camera SDKがインストールされていない場合はerrorになり、ヒント情報でインストール方法が示されます。

```python
result = connect_camera()
# Example result
{
  "ok": true,
  "code": "OK",
  "message": "Camera connected",
  "data": {
    "camera_name": "FUJIFILM X-S10/XF35mmF2 R WR",
    "camera_id": "0"
  },
  "error": null,
  "meta": {
    "tool": "connect_camera",
    "timestamp": "2026-10-13T01:27:57.202386+00:00"
  }
}
```

#### disconnect_camera()
カメラとの通信接続を終了します。
connect/disconnect はオーバーヘッドを伴うため、なるべく接続を維持するようにしてください。

```python
disconnect_camera()
```

#### get_camera_status()
カメラの接続状態・撮影Readyか否か・撮影設定（露出とフォーカスに関する設定）を返します。

```python
result = get_camera_status()
```

### 設定オプションの取得

#### get_program_mode_options()
利用可能なプログラムモード（P/A/S/M）を取得します。
```python
result = get_program_mode_options()
# Example result
{
  "ok": true,
  "code": "OK",
  "message": "Program mode options retrieved",
  "data": {
    "program_mode": "P",
    "options": [ "M", "A", "S", "P" ]
  },
  "error": null,
  "meta": {
    "tool": "get_program_mode_options",
    "timestamp": "2026-10-13T01:40:47.790973+00:00"
  }
}
```

#### get_exposure_compensation_options()
露出補正の選択肢を取得します。
取得したオプションは、プログラムモード　`P`、`A`、`S` の場合に利用可能です。
```python
result = get_exposure_compensation_options()
```

#### get_iso_sensitivity_options()
ISO感度の選択肢を取得します。
取得したオプションは、全てのプログラムモードで利用可能です。
```python
result = get_iso_sensitivity_options()
```

#### get_aperture_options()
絞り値の選択肢を取得します。
取得したオプションは、プログラムモード　`A`、`M` の場合に利用可能です。
```python
result = get_aperture_options()
```

#### get_shutter_speed_options()
シャッター速度の選択肢を取得します。
取得したオプションは、プログラムモード　`S`、`M` の場合に利用可能です。
```python
result = get_shutter_speed_options()
```

#### get_focus_mode_options()
利用可能なフォーカスモード（MF/AF-S/AF-C）を取得します。
```python
result = get_focus_mode_options()
```

#### get_focus_distance_options()
MF（マニュアルフォーカス）時に指定できる距離値の範囲を取得します。
カメラに装着したレンズに依存する内部値(int) でINF（無限遠）とMOD（最短距離）を表します。
```python
result = get_focus_distance_options()
```

#### get_focus_point_options()
フォーカスポイントの選択肢を取得します。
画面中心のフォーカスポイントを (h=0, v=0) で表し、左下がマイナス、右上がプラスです。
選択できるポイントの座標範囲はカメラのセンサーや設定によって異なります。
```python
result = get_focus_point_options()
```

### 露出設定

#### set_exposure_automatic(exposure_compensation: Optional[str] = None)
プログラムモード `P` に設定し、必要に応じて露出補正を設定します。
```python
result = set_exposure_automatic()
result = set_exposure_automatic(exposure_compensation="+1.0EV")
# Example result
{
  "ok": true,
  "code": "OK",
  "message": "Exposure set to automatic mode",
  "data": {
    "requested": {
      "program_mode": "P",
      "exposure_compensation": "+1.0EV"
    },
    "applied": {
      "program_mode": "P",
      "aperture": "Auto (determined by the camera at the moment of capture)",
      "shutter_speed": "Auto (determined by the camera at the moment of capture)",
      "exposure_compensation": "+1.0EV"
    },
    "warnings": [
      "In program mode 'P', shutter_speed and aperture are determined by the camera according to the brightness of the subject at the moment of capture. It cannot be read in advance, and get_camera_status reports it as 'Auto (determined by the camera at the moment of capture)'. Use take_preview() or take_photo() to see the result of the current settings."
    ]
  },
  "error": null,
  "meta": {
    "tool": "set_exposure_automatic",
    "timestamp": "2026-10-13T01:42:56.811813+00:00"
  }
}
```

#### set_exposure_shutter_priority(shutter_speed: str, exposure_compensation: Optional[str] = None)
プログラムモード `S` に設定し、シャッター速度を設定します。
```python
result = set_exposure_shutter_priority(shutter_speed="1/60")
result = set_exposure_shutter_priority(shutter_speed="1/60", exposure_compensation="+1.0EV")
```

#### set_exposure_aperture_priority(aperture: str, exposure_compensation: Optional[str] = None)
プログラムモード `A` に設定し、絞り値を設定します。
```python
result = set_exposure_aperture_priority(aperture="F2.8")
result = set_exposure_aperture_priority(aperture="F2.8", exposure_compensation="-1.0EV")
```

#### set_exposure_manual(shutter_speed: str, aperture: str)
プログラムモード `M` に設定し、シャッター速度と絞り値を設定します。
```python
result = set_exposure_manual(shutter_speed="1/60", aperture="F2.8")
```

#### set_iso_sensitivity(iso_sensitivity: str)
全てのプログラムモードに共通のISO感度を設定します。
```python
result = set_iso_sensitivity(iso_sensitivity="ISO 100")
```


### フォーカス設定

#### set_manual_focus_mode(mf_distance: Optional[int] = None)
マニュアルフォーカス `MF` に切り替え、フォーカス距離を設定します。
```python
result = set_manual_focus_mode()
result = set_manual_focus_mode(mf_distance=100)
# Example result 
{
  "ok": true,
  "code": "OK",
  "message": "Manual focus mode enabled",
  "data": {
    "requested": { "focus_mode": "MF" },
    "applied": { "focus_mode": "MF", "mf_distance": 664 },
    "options": { "INF": 22, "MOD": 1402, "STEP": 5 },
    "warnings": []
  },
  "error": null,
  "meta": {
    "tool": "set_manual_focus_mode",
    "timestamp": "2026-10-13T01:48:02.414315+00:00"
  }
}
```

#### set_auto_focus_mode(focus_mode: str, af_area_mode: Optional[str] = None)
`focus_mode` でオートフォーカスの種類 `AF-S` / `AF-C` を設定します。
`af_area_mode` を指定する場合は `SINGLE` / `ZONE` / `WIDETRACKING` のいずれかを指定します。
```python
result = set_auto_focus_mode(focus_mode="AF-S", af_area_mode="SINGLE")
result = set_auto_focus_mode(focus_mode="AF-C", af_area_mode="ZONE")
```

#### set_focus_point(h: int, v: int)
指定した座標にフォーカスポイントを移動したのち、フォーカスを合わせます。
マニュアルフォーカスモード、もしくはオートフォーカスモードでAFモードが `SINGLE` の場合に有効です。
座標はファインダー内の位置を表し、スクリーンの中心が `(h=0, v=0)` です。
`h` は水平方向、`v` は垂直方向を表し、**`+h` が右、`+v` が上**（左下がマイナス、右上がプラス）です。
移動後に何にピントが合ったかは `check_target_image()` で確認できます。
```python
result = set_focus_point(h=0, v=0)
# Example result
{
  "ok": true,
  "code": "OK",
  "message": "Focus point updated",
  "data": {
    "requested": { "h": 3, "v": 3 },
    "applied": { "h": 3, "v": 3 },
    "options": {
      "h": [ -6, -5, ... ], "v": [ -4,...,4 ],
      "orientation": "+h is right, -h is left, +v is up, -v is down (origin is the center of the screen)"
    },
    "warnings": []
  },
  "error": null,
  "meta": {
    "tool": "set_focus_point",
    "timestamp": "2026-10-13T01:50:53.219469+00:00"
  }
}
```

### 撮影

#### view_finder_image(size: Optional[str] = None, temp_filename: Optional[str] = None)
カメラのファインダーに表示されているライブビュー画像を１コマ取得します。
現在の視界に何が映っているかを確認できます。
小さいJPEG画像をファイルまたはデータ（base64エンコードされた文字列）で受け取ります。
```python
result = view_finder_image()
result = view_finder_image(size="S")
result = view_finder_image(size="M", temp_filename="temp.jpg")
# Example result
{
  "ok": true,
  "code": "OK",
  "message": "Finder image captured",
  "data": {
    "requested": { "size": "S", "temp_filename": null },
    "applied": {
      "image": "/9j/xAGiAAABBQEBA/j........",
      "filename": ""
    },
    "warnings": []
  },
  "error": null,
  "meta": {
    "tool": "view_finder_image",
    "timestamp": "2026-10-13T01:57:27.516297+00:00"
  }
}
# Example result with temp_filename provided
{
  ...
  "data": {
    "requested": { "size": "M", "temp_filename": "finder_M.jpg" },
    "applied": {
      "image": "",
      "filename": "finder_M.jpg"
    },
  }
  ...
}
```

#### check_target_image(size: Optional[str] = None, temp_filename: Optional[str] = None, zoom_selector: int=1)
現在フォーカスポイントがある位置のライブビュー画像をクローズアップして取得します。
何にピントが合っているかを確認できます。
小さいJPEG画像をファイルまたはデータ（base64エンコードされた文字列）で受け取ります。
```python
result = check_target_image()
result = check_target_image(size="S")
result = check_target_image(size="M", temp_filename="temp.jpg", zoom_selector=2)
```

#### take_preview(size: Optional[str] = None, temp_filename: Optional[str] = None)
現在の撮影設定でテストショットを実行し、保存せずに確認画像を取得します。
小さいJPEG画像をファイルまたはデータ（base64エンコードされた文字列）で受け取ります。
```python
result = take_preview()
result = take_preview(size="S")
result = take_preview(size="M", temp_filename="temp.jpg")
```

#### take_photo(folder: Optional[str] = None, successive_shots:Optional[int] = 1)
現在の撮影設定で撮影を実行し、保存先フォルダとファイル一覧を返します。
`folder` で保存先フォルダを指定できます。指定したフォルダがない場合は自動的に作成されます。
`successive_shots` で2以上を指定すると連続撮影を行います。
保存した最後のJPEGのEXIFから、**実際に使われた露出**を `applied["exposure_used"]` で返します。
`P`/`A`/`S` モードでカメラが撮影時に決めたシャッター速度などを知る唯一の方法です。
ただしこれは実測値であり設定値ではありません。カメラの選択肢に存在しない値（例: `1/105`）になることがあるため、
同じ露出を`M` モードで再現するには `get_*_options()` の中から最も近い値を選び直す必要があります。
キー名は設定用の引数と区別できるよう単位付き（`shutter_speed_sec`, `aperture_f_number`, `focal_length_mm` 等）にしています。
```python
result = take_photo()
result = take_photo(folder="captured_images", successive_shots=3)
# Example result
{
  ...
  "data": {
    "requested": { "folder": null, "successive_shots": 3 },
    "applied": {
      "folder": "actual/folder/path",
      "files": [ "2026_1013_1104-5265.jpg" ],
      "count": 1,
      "exposure_used": {
        "source": "EXIF of 2026_1013_1104-5265.jpg",
        "shutter_speed_sec": 0.009524,
        "shutter_speed_text": "~1/105",
        "aperture_f_number": 2,
        "iso": 1000,
        "exposure_compensation_ev": 1,
        "focal_length_mm": 35,
        "note": "Measured from the captured image (EXIF). These are results, not setting values: passing them to set_* tools may fail because the exact value is not always in the camera's option list. To reproduce this exposure, pick the nearest value from get_shutter_speed_options() / get_aperture_options() / get_iso_sensitivity_options()."
      }
    },
}
{
  ...
  "data": {
    "requested": { "folder": "captured_images", "successive_shots": 3
    },
    "applied": {
      "folder": "captured_images",
      "files": [
        "2026_1013_1106-4376.jpg",
        "2026_1013_1106-4397.jpg",
        "2026_1013_1106-4421.jpg"
      ],
      "count": 3,
      "exposure_used": {
        "source": "EXIF of 2026_1013_1106-4421.jpg",
        ...
      }
    },
  },
}
```

#### bulb_exposure(folder: Optional[str] = None, exposure_time: int = 1)
バルブ撮影を実行し、保存先フォルダとファイル一覧を返します。
`folder` で保存先フォルダを指定できます。指定したフォルダがない場合は自動的に作成されます。
`exposure_time` で露出時間を秒単位で指定します。
非同期で実行されます。この呼び出しをキャンセルするとその時点で露出を止めて画像を保存します。
呼び出し時の撮影設定は一時的に退避してバルブ撮影用の設定に切り替え、終了後に元の設定に戻します。
```python
result = bulb_exposure(folder="captured_images", exposure_time=30)
```

#### cancel_bulb_exposure()
実行中のバルブ撮影を中止（キャンセル）し、画像は保存しません。
予定時間よりも早く終了して保存したい場合は、`bulb_exposure` リクエスト自体をキャンセルします。

## 使用例

```
# カメラに接続して現在の状態を取得する
connect_camera()
get_camera_status()

# 絞り値の選択肢と露出補正の選択肢を確認し、カメラを絞り優先モードで明るめに設定する
get_aperture_options()
get_exposure_compensation_options()
:
set_exposure_aperture_priority( aperture="F2.0", exposure_compensation="+1.0EV" )

# フォーカスポイントを右上に移動してその位置の被写体をCloseupして確認する
set_auto_focus_mode( focus_mode="AF-S", af_area_mode="SINGLE" )
get_focus_point_options()   # フォーカスポイント h,v の選択肢を取得する
set_focus_point(h=3, v=2)
check_target_image(size="S", zoom_selector=2, temp_filename="target_h3v2_s.jpg")
:

# 写真を撮影する（ファイルに保存される）
take_photo( folder="captured_images" )

# バルブ撮影を行う
bulb_exposure( folder="captured_images", exposure_time=30 )
```

## プロジェクト構造

```
fujixcam-mcp/
├── src/
│   └── fujixcam_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       └── server.py       # MCPサーバー実装
├── tests/
│   └── server/        # MCP inspector x MCP server テスト環境
│   │   └── mcpinspector_and_server.bat
│   └── agents/        # AI Agents x MCP server テスト環境
│       ├── CLAUDE.md
│       ├── GEMINI.md
│       ├── AGENTS.md
│       ├── PROJECT_RULES.md
│       └── SESSION_nn.md   # AIエージェントとのセッション定義サンプル
├── .gitignore
├── .mcp.json              # MCP設定ファイル例
├── pyproject.toml
└── README.md
```

## 開発

### MCP Inspectorでのデバッグ

```bash
npx @modelcontextprotocol/inspector python -m fujixcam_mcp.server
```

### 既知の問題
- macOSではバルブ撮影が起動できません（原因調査中）(X-S10, MacBookAir/M1-MacOS26.7.1, Python 3.12.15, 3,14.8)
- 全てのカメラモデルで確認を行っているわけではありません。同じ操作でも挙動が異なり期待通りに動作しない場合があります。

## トラブルシューティング

### カメラが検出されない
- カメラが正しく接続されているか確認してください
- カメラの設定がUSBテザーモードになっているか確認してください
- 規格に準拠したUSBケーブルを使用しているか確認してください
- PCのセキュリティ設定が外部機器の接続を妨げていないか確認してください
- 他のアプリケーションがカメラを使用していないか確認してください

### その他の問題
- FUJIFILMのカメラSDKが正しくインストールされているか確認してください
- 撮影画像の保存先フォルダに書き込み権限があるか確認してください
- カメラのオートパワーオフが無効になっているか確認してください

## ライセンス

MIT License

## 作者
Masahiro Terada

Created for MCP Windows/macOS digital camera control integration.
