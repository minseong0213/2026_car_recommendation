# 2026 Car Recommendation

V5 기술설계서 흐름을 바탕으로 실제 주행 OBD/GPS 데이터를 분석해 운전자 성향, 경사/외부부하 특성, 현대/기아/제네시스 차량 추천 결과를 생성하는 파이프라인입니다.

프로젝트별 변경 내역은 [CHANGELOG.md](CHANGELOG.md)에서 확인할 수 있습니다.

## 현재 적용 상태

| 기능 | 상태 | 현재 범위 |
| --- | --- | --- |
| 차량 추천 파이프라인 | 구현 | 주행 CSV 분석, 운전자 성향 계산, 차량 DB 기반 추천 결과 생성 |
| 직접 CSV 추천 API | 구현 | `POST /recommendations/upload`로 업로드한 CSV를 즉시 분석 |
| S3 업로드 연동 | 구현 | Presigned URL 발급, 완료 처리, 추천 실행 및 최근 로그 조회 |
| 경고등 진단 API | 부분 구현 | 알려진 DTC 4종의 전용 진단과 그 외 코드의 공통 진단 제공 |
| PID 기반 원인 재정렬 | 미구현 | PID 값을 요청·응답에 포함하지만 현재 원인 선택과 순위에는 미사용 |
| DTC 삭제 | 미구현 | 진단 조회 API만 제공하며 차량 ECU의 코드 삭제는 수행하지 않음 |

## 구성

- `recommendation_pipeline.py`: 차량 DB와 실제 주행 CSV를 읽어 주행 특성 및 차량 추천 점수를 계산합니다.
- `api_server.py`: FastAPI 기반 추천 API 서버입니다. CSV 경로 또는 업로드 파일을 받아 추천 결과 JSON을 반환합니다.
- `analyze_grade_characteristics.py`: GPS 고도값을 사용하지 않고 차량속도, 엔진부하, RPM, MAF, 연료량 기반으로 경사/외부부하 의심 구간을 분석합니다.
- `generate_route_map.py`: GPS 좌표는 위치 표시용으로만 사용하고, OBD 기반 경사/부하 판정 결과를 지도에 오버레이합니다.
- `outputs/`: 테스트 주행 데이터 기반 추천 리포트, 경사 분석 리포트, 지도 HTML/GeoJSON/PNG 산출물이 들어 있습니다.

## 주요 산출물

- `outputs/drive_2026-05-01_15-50-54/recommendation_test_report.md`
- `outputs/drive_2026-05-01_15-50-54/grade_analysis_report.md`
- `outputs/drive_2026-05-01_15-50-54/drive_route_map.html`
- `outputs/drive_2026-05-01_15-50-54/vehicle_recommendation_results.csv`

## 실행 예시

```powershell
python recommendation_pipeline.py `
  --vehicle-db "C:\path\to\현대기아_제네시스_파워트레인별_차량DB_v2.xlsx" `
  --drive-csv "C:\path\to\drive.csv" `
  --output-dir ".\outputs\drive_example"

python analyze_grade_characteristics.py `
  --drive-csv "C:\path\to\drive.csv" `
  --output-dir ".\outputs\drive_example"

python generate_route_map.py `
  --drive-csv "C:\path\to\drive.csv" `
  --output-dir ".\outputs\drive_example" `
  --grade-timeseries ".\outputs\drive_example\obd_grade_timeseries.csv"
```

## API 서버 실행

```powershell
$env:VEHICLE_DB_PATH="C:\path\to\현대기아_제네시스_파워트레인별_차량DB_v2.xlsx"
$env:DRIVE_DATA_S3_BUCKET="your-drive-data-bucket"
$env:DRIVE_DATA_S3_PREFIX="drive-logs"
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

주요 엔드포인트:

- `GET /health`: 서버 상태와 차량 DB 경로 확인
- `GET /vehicles/summary`: 차량 DB 개수와 브랜드/파워트레인 요약
- `POST /warning-lights/diagnose`: DTC와 실시간 PID 스냅샷을 받아 주요 원인 5개와 예상 수리 견적 반환
- `POST /recommendations`: 서버 로컬 CSV 경로를 입력해 추천 실행
- `POST /recommendations/upload`: CSV 파일 업로드 후 추천 실행
- `POST /drive-uploads/presign`: GUI/앱이 S3에 직접 CSV를 올릴 수 있는 presigned PUT URL 발급
- `POST /drive-uploads/complete`: S3 업로드 완료 후 해당 CSV를 내려받아 추천 실행
- `GET /drive-uploads/logs`: 최근 S3 업로드/CSV 분석/추천 실행 로그 확인
- `POST /recommendations/s3`: S3 CSV 경로를 입력해 추천 실행

## 경고등 진단 적용 범위

“알려진 DTC 4종”은 백엔드의 진단 카탈로그에 코드별 전용 내용이 등록되어 있다는
뜻입니다. 해당 코드가 들어오면 코드에 맞게 작성된 주요 예상 원인 5개, 상세 설명,
점검 방법과 예상 수리 견적을 정해진 우선순서로 반환합니다.

| DTC | 의미 | 경고등 | 전용 진단의 주요 범위 |
| --- | --- | --- | --- |
| `P0300` | 무작위/복수 실린더 실화 | 엔진 경고등 | 점화 코일, 점화 플러그, 인젝터, 진공 누설, 연료 압력 |
| `P0171` | 혼합비 희박(Bank 1) | 엔진 경고등 | 흡기 누설, MAF 센서, 연료 압력, 산소 센서, 인젝터 |
| `P0420` | 촉매 정화 효율 저하(Bank 1) | 엔진 경고등 | 촉매, 후단 산소 센서, 배기 누설, 실화, 혼합비 제어 |
| `P0562` | 시스템 전압 낮음 | 배터리 경고등 | 발전기, 배터리, 구동 벨트, 단자, 충전 회로 배선 |

“미등록 코드 공통 진단”은 위 4종 이외의 DTC가 들어왔을 때 API 요청을 실패시키지
않기 위한 fallback입니다. 백엔드는 입력 코드를 정규화해 응답에 그대로 포함하지만,
그 코드의 제조사별 의미를 해석하지는 않습니다. 대신 모든 미등록 코드에 다음 공통
후보 5개를 반환합니다.

1. 관련 센서 이상
2. 배선 및 커넥터 접촉 불량
3. 관련 액추에이터 성능 저하
4. 오일·냉각수·연료 상태 이상
5. 제어 모듈 또는 소프트웨어 이상

따라서 미등록 코드 결과는 구체적인 진단이 아니라 정비소 점검 방향을 제공하는
임시 안내입니다. 현재 공통 진단은 `엔진 경고등`으로 표시되므로 ABS, 변속기,
차체 및 제조사 전용 코드까지 경고등 종류를 정확히 구분하지 못할 수 있습니다.
또한 요청의 `pid_values`는 응답에 포함되지만 아직 원인 선택이나 순위를 변경하지
않습니다. Flutter 앱은 사용자 화면에서 DTC와 PID를 숨기고 경고등 명칭과 안내만
표시합니다.

`POST /recommendations` 예시:

```json
{
  "drive_csv_path": "C:\\path\\to\\drive.csv",
  "save_artifacts": true,
  "output_dir": ".\\outputs\\api\\drive_example",
  "top_n": 10,
  "constraints": {
    "budget_min_10k_krw": 3000,
    "budget_max_10k_krw": 6000,
    "passengers": 4,
    "cargo_need": "보통",
    "monthly_distance_km": 1200,
    "apply_body_filter": true
  }
}
```

`POST /warning-lights/diagnose` 예시:

```json
{
  "dtc_code": "P0300",
  "pid_values": {
    "rpm": 812,
    "coolantC": 88,
    "engineLoadPct": 24.5,
    "mafGs": 3.8
  }
}
```

응답에는 경고등/고장 코드 설명, 카탈로그에 정해진 순서의 원인 5개, 원인별 상세
점검 항목과 만원 단위 예상 견적 범위가 포함됩니다. DTC는 전용 카탈로그를 선택하는
데 사용합니다. PID 값은 현재 응답에 보존만 하며 진단 로직에는 아직 반영하지 않습니다.

`POST /drive-uploads/presign` 예시:

```json
{
  "filename": "obd_log_20260504_120000.csv",
  "content_type": "text/csv",
  "expires_in_s": 900
}
```

응답의 `upload_url`로 CSV를 `PUT` 업로드한 뒤, 응답에 포함된 `key`를
`POST /drive-uploads/complete` 또는 `POST /recommendations/s3`의 `s3_key`로
넘기면 추천을 실행할 수 있습니다.

개발 중에는 서버 콘솔에 `[drive-upload]` 로그가 찍힙니다. 예를 들면
`presign.created`, `complete.received`, `s3.downloaded`, `recommendation.done`
순서로 S3 key, CSV row 수, 주행거리, 연료소비율, 추천 1순위가 보입니다.
브라우저나 PowerShell에서 최근 로그를 JSON으로 확인할 수도 있습니다.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/drive-uploads/logs?limit=20"
```

운영 환경에서는 원본 CSV를 S3에 저장하고, API 서버/워커가 내려받은 서버 로컬 경로를 `/recommendations`에 넘기는 구조를 권장합니다. GUI/앱에는 AWS access key를 넣지 말고, 추천 API 서버가 presigned URL을 발급하게 두는 방식이 안전합니다.

## 주의

- 지도/GeoJSON 산출물에는 실제 이동 경로 좌표가 포함됩니다. 공개 저장소에 올릴 때는 좌표 파일을 제외하거나 익명화해야 합니다.
- 경사 분석은 실제 도로 경사도 실측값이 아니라 OBD 기반 `엔진부하 - 속도별 기대부하` 편차를 이용한 추정입니다.
- 정차/초저속 구간은 현재 신뢰도 문제로 대부분 분석 제외 처리됩니다.
