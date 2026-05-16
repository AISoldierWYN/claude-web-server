# Rule Builder Evaluation Cases

This document defines the lightweight evaluation case format used by:

```bash
python skills/android-log-rule-builder/scripts/rule_pack_manager.py evaluate --eval-root android_analysis_eval
```

Large external repositories and raw logs should stay outside Git:

```text
tests/github_apps/<owner>__<repo>/
tests/android_eval_artifacts/
```

Only small case metadata and small sanitized fixtures should be committed.

## Directory Layout

```text
android_analysis_eval/
  cases/
    <case-id>/
      case.json
      expected.json
      question.md
      notes.md
      optional-small-log.txt
```

`question.md` and `notes.md` are informational for humans and future full-chain Android analysis. The Stage 1 `evaluate` command uses `case.json` and `expected.json`.

## case.json

```json
{
  "id": "rdm-functional-lock",
  "repo": "local/RealtimeDeviceManager",
  "commit": "optional-fixed-commit",
  "project_preset": "app",
  "bundle_id": "android-rdm",
  "rule_pack_id": "rdm-generated",
  "profiles": ["functional"],
  "project_dirs": ["D:/AndroidCode/RealtimeDeviceManager"],
  "log_path": "rdm.log"
}
```

Fields:

| Field | Required | Description |
|---|---:|---|
| `id` | no | Stable case id. Defaults to the case directory name. |
| `repo` | no | Source repo label, for example `android/nowinandroid`. |
| `commit` | no | Pinned commit for reproducibility. |
| `project_preset` | no | Future generation preset: `app`, `framework`, `native`, or `library`. |
| `bundle_id` | yes | Bundle id in `claude_web_paths.config.json`. |
| `bundle` | no | Bundle metadata used to synthesize a temporary paths config when `bundle_id` is not in `--paths-config`. |
| `rule_pack_id` | no | Rule pack id. Defaults to `<bundle-short-name>-generated`. |
| `generated_rule_pack` | no | Alias for `rule_pack_id`, useful when mirroring plan docs. |
| `profiles` | no | Expected issue profiles covered by this case. |
| `project_dir` | no | One project path to scan. |
| `project_dirs` | no | Multiple project paths to scan. |
| `log_path` | no | Log file/directory/zip used by the lightweight rule hit test. |
| `log_archive` | no | Alias for `log_path`. |

Relative paths are resolved against the case directory first, then the repository root. A `local://` prefix is resolved against the repository root, for example:

```json
{
  "log_archive": "local://tests/android_eval_artifacts/nia-functional.zip"
}
```

If the normal `--paths-config` does not contain the case `bundle_id`, `evaluate` can synthesize a temporary paths config from `case.json`:

```json
{
  "bundle": {
    "title": "AntennaPod",
    "summary": "Podcast app playback/download signals",
    "keywords": ["AntennaPod", "PlaybackService"]
  },
  "project_dirs": ["local://tests/github_apps/AntennaPod__AntennaPod"]
}
```

The generated temp config is written under the selected knowledge dir in `_eval_paths/`.

## expected.json

```json
{
  "min_rule_count": 3,
  "min_hit_count": 1,
  "expected_keywords": ["RDM", "DeviceLock"],
  "must_hit_rule_tags": ["business"],
  "must_hit_terms": ["lock failed"],
  "must_not_hit_terms": ["unrelated-crash"],
  "max_generic_term_ratio": 0.2
}
```

Fields:

| Field | Description |
|---|---|
| `min_rule_count` | Minimum number of generated rules. |
| `min_hit_count` | Minimum number of lightweight log hits. |
| `expected_profile` / `expected_profiles` | Profile(s) that must appear in generated `metadata.profiles`. |
| `expected_keywords` | Terms that must appear somewhere in the generated rule pack. |
| `must_hit_rule_tags` | Rule tags that must appear among rules hit by `log_path`. |
| `must_hit_terms` | Terms that must appear in lightweight hit output. |
| `must_not_hit_terms` | Terms that must not appear in lightweight hit output. |
| `max_generic_term_ratio` | Warning threshold for pure generic terms like `error`, `fail`, `exception`. |

## Scorecard

The command writes `scorecard.json`:

```json
{
  "version": 1,
  "ok": true,
  "case_count": 1,
  "passed_count": 1,
  "failed_count": 0,
  "cases": [
    {
      "id": "rdm-functional-lock",
      "ok": true,
      "metrics": {
        "schema_ok": true,
        "rule_count": 4,
        "hit_count": 2,
        "generic_term_ratio": 0.0
      }
    }
  ]
}
```

Stage 1 only evaluates the rule builder loop. Later stages should extend this scorecard with full Android analysis metrics such as selected profile, selected bundle, Evidence Pack precision, Deep trigger decision, and final conclusion checks.

## Profile Notes

When `case.json` includes `profiles`, `evaluate` passes each item to `generate` as `--profile`:

```json
{
  "profiles": ["functional", "stability"]
}
```

Generated rule packs should then contain `metadata.profiles` and profile-specific rule ids such as:

```text
<rule-pack-id>-functional-flow-signals
<rule-pack-id>-stability-crash-signals
<rule-pack-id>-xts-test-signals
<rule-pack-id>-memory-signals
<rule-pack-id>-performance-signals
```

The generated `bundle.json` should also include `supported_profiles` and `profile_overrides` so later routing can prefer a profile-specific rule pack instead of loading every rule equally.

## APP Preset Notes

When `project_preset` is `app`, `evaluate` passes `--project-preset app` to `generate`. The rule builder should extract ordinary Android App signals such as Manifest components, permissions, Gradle modules, WorkManager, Retrofit/OkHttp, Room/SQLite/DataStore, FileProvider/ContentResolver, NotificationManager, and BuildConfig/RemoteConfig.

Use `expected_keywords` and `must_hit_rule_tags` to ensure these signals are not silently lost:

```json
{
  "expected_keywords": ["SyncWorker", "Retrofit", "android.permission.POST_NOTIFICATIONS"],
  "must_hit_rule_tags": ["app", "business"]
}
```
