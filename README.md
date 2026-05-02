# 2026 Car Recommendation

V5 기술설계서 흐름을 바탕으로 실제 주행 OBD/GPS 데이터를 분석해 운전자 성향, 경사/외부부하 특성, 현대/기아/제네시스 차량 추천 결과를 생성하는 파이프라인입니다.

## 구성

- `recommendation_pipeline.py`: 차량 DB와 실제 주행 CSV를 읽어 주행 특성 및 차량 추천 점수를 계산합니다.
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

## 주의

- 지도/GeoJSON 산출물에는 실제 이동 경로 좌표가 포함됩니다. 공개 저장소에 올릴 때는 좌표 파일을 제외하거나 익명화해야 합니다.
- 경사 분석은 실제 도로 경사도 실측값이 아니라 OBD 기반 `엔진부하 - 속도별 기대부하` 편차를 이용한 추정입니다.
- 정차/초저속 구간은 현재 신뢰도 문제로 대부분 분석 제외 처리됩니다.
