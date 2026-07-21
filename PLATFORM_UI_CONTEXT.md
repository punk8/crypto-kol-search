# Platform UI template context

`platform_center.html` and `platform_workspace.html` are presentation-only templates. Routes should normalize registry/domain objects into the mappings below; templates never query repositories or compare metrics across platforms.

## `platform_center.html`

- `urls.kill_switch`: POST endpoint for the global kill switch.
- `urls.discovery`: anchor or workspace URL for starting discovery on the first enabled platform that declares a discovery capability.
- `global_kill_switch`: current global write-pause state.
- `capability_labels`: mapping from capability ID to localized label.
- `summary`: `enabled_platforms`, `total_platforms`, `active_tasks`, `queued_tasks`, `failed_tasks`, `open_opportunities`, `alert_count`, and `paused_channels`.
- `platforms[]`: `platform_id`, `name`, `short_name`, `version`, `enabled`, `connection_status`, optional `connection_message`, `workspace_url`, capability ID list, optional `last_scan_at`, and a `metrics` mapping with `active_kols`, `open_opportunities`, `succeeded_actions_today`, and `paused_channels`.
- `alerts[]`: `severity`, `title`, `message`, `platform_name`, `created_at`, and `href`.
- `recent_runs[]`: core job `id`, `platform_short_name`, `label`, `created_at`, progress percentage, `status`, and an anchor-style `detail_url`; this is not the removed `/runs` route.

Stored connection states are `connected`, `degraded`, `disconnected`, or `paused`; the presentation layer renders `paused` as `disabled`. Values shown on the platform center are operational counts only and must not be used as a cross-platform influence ranking.

## `platform_workspace.html`

- `platform`: `platform_id`, `name`, `short_name`, `version`, `accent_color`, `description`, `connection_status`, optional `connection_message`, `kill_switch`, capability ID list, `can_scan`, `scan_interval_label`, and `safety_summary`.
- `urls`: `center`, `scan`, `kill_switch`, `opportunities`, `discover`, `dm`, `seeds`, `connections`, `kols`, `accounts`, `actions`, and `runs`. The collection-style values currently resolve to section anchors; no legacy list route is implied.
- `metrics`: `active_kols`, `review_kols`, `open_opportunities`, `planned_opportunities`, `succeeded_actions_today`, `review_actions`, and `next_scan_at`.
- `discovery`: `default_query` and `default_limit`.
- `all_capabilities[]`: `id` and fallback `label`; this renders enabled and unavailable capabilities explicitly.
- `alerts[]`: `severity`, `title`, `message`, and `href`.
- `opportunities[]`: normalized priority (0–100), localized `kind_label`, `title`, `summary`, shared opportunity `status`, evidence `reasons`, `native_object_label`, optional `native_url`, `created_at`, review fields, and dismissal fields (`dismiss_url`, `can_dismiss`).
- `kols[]`: `detail_url`, `status_url`, `initials`, `display_name`, platform-native `handle` and `native_id`, platform-local `score`, KOL `status`, and score `reasons`.
- `accounts[]`: `connection_id`, `display_name`, `external_account_id`, role label list, connection `kind`, channel `status`, `paused`, and optional `last_health_error`.
- `action_controls[]`: `action_type`, localized `label`, and `paused` for capabilities that map to writable action types.
- `recent_actions[]`: detail/approval/cancel/resolve URLs, localized `kind_label`, `target_label`, `updated_at`, shared action `status`, draft and policy explanation, plus optional receipt/error fields.
- `recent_runs[]`: core job `detail_url`, `label`, `id`, `created_at`, and `status`.

Capability IDs must use the registry values: `account_search`, `content_search`, `timeline/feed`, `relations`, `native_trends`, `comment`, `dm`, `owned_publish`, `media_upload`, and `analytics`. `all_capabilities` intentionally includes unavailable contract capabilities so the matrix can label them “未接入”; neither current built-in platform declares `media_upload`. Search controls are rendered only when `account_search`, `content_search`, or `relations` is present. Routes should only provide review/action URLs for operations supported by the platform.

Until `base.html` gains an optional stylesheet block, both templates load `/static/platforms.css` inside their content block. At integration time this link may be moved to the shared head without changing the templates' context contract.
