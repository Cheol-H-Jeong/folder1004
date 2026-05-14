# Folder Profile & Health Module Spec

## 목적
선택한 폴더의 성격과 정리 필요도를 LLM 호출 없이 로컬에서 계산해, 사용자가 별도 선택을 하지 않아도 Folder1004가 알맞은 분류 스타일을 추천하고 실행 리포트에 근거를 남긴다.

## 모듈

### `folder1004.folder_profile`
입력:
- `root: Path`
- `entries: list[FileEntry]`
- `recursive: bool`

출력: `FolderProfileSummary`
- `profile_id`: 안정적인 영문 ID. 예: `downloads`, `photos`, `school`, `business`, `research`, `code`, `media`, `documents`, `mixed`.
- `label`: 한국어 표시명.
- `confidence`: `0.0..1.0`.
- `matched_signals`: 어떤 단서로 판단했는지 사람이 읽을 수 있는 짧은 문자열 목록.
- `recommended_preset_names`: `CLASSIFICATION_GUIDANCE_PRESETS`의 label 목록만 포함. 실제 프롬프트 본문은 포함하지 않는다.
- `health_score`: `0..100`; 높을수록 정돈됨, 낮을수록 정리 필요.
- `health_level`: `좋음 | 보통 | 정리 필요 | 심각`.
- `health_reasons`: 점수를 깎은 근거.
- `file_count`, `root_file_count`, `total_bytes`, `extension_counts`.

규칙:
- 네트워크/LLM/키링 접근 금지.
- 존재하지 않는 파일은 무시하지 않고 metadata 단계에서 이미 걸러진 entries만 사용.
- 프로필 추천은 preset label만 저장/전달한다.
- 동률이면 더 구체적인 프로필(photos/school/business/research/code/downloads) 우선, 없으면 `mixed`.

### `folder1004.models.OperationResult`
- `folder_profile: FolderProfileSummary | None` 필드를 가진다.
- 기존 호출자는 값을 넣지 않아도 동작해야 한다.

### `folder1004.pipeline.run`
- `gather_entries` 직후 `analyze_folder_profile` 실행.
- progress 콜백에 프로필/건강 점수 1줄 표시.
- `OperationResult` 반환 전에 `op.folder_profile` 설정.

### `folder1004.reporter`
- Markdown 리포트 상단에 폴더 프로필, 추천 스타일, 건강 점수, 근거를 표시.
- preset 본문은 절대 출력하지 않는다.

## 테스트
- Downloads 성격 폴더: 설치 파일/zip/tmp 이름이 있으면 `downloads`, 추천에 `버림 후보 분리` 포함.
- 사진 폴더: jpg/png/photo/customer 단서가 있으면 `photos`, 추천에 `사람/고객 중심`, `날짜/기간 중심` 포함.
- 건강 점수: 루트 파일/설치 파일/임시 파일이 많으면 점수가 낮아지고 reason이 생긴다.
- 파이프라인 mock run: `OperationResult.folder_profile`이 채워지고 리포트에 프로필/건강 점수가 나온다.
