-- One row per GitLab issue event. event_key is GitLab's Idempotency-Key,
-- which stays the same when GitLab retries a delivery.
create schema if not exists triage;

create table if not exists triage.issue_triage (
  id                bigserial primary key,
  event_key         text not null unique,
  project_id        bigint not null,
  issue_iid         integer not null,
  issue_url         text,
  title             text,
  status            text not null default 'received'
    check (status in ('received', 'classified', 'auto_applied', 'waiting_review',
                      'approved', 'corrected', 'rejected', 'expired')),
  route             text check (route in ('auto', 'needs_info', 'review', 'security')),
  route_reason      text,
  decision_model    text,
  decision_answers  jsonb,
  suggested_labels  text[],
  draft_comment     text,
  final_labels      text[],
  posted_comment    text,
  received_at       timestamptz not null default now(),
  classified_at     timestamptz,
  decided_at        timestamptz
);

create index if not exists issue_triage_status_idx on triage.issue_triage (status);

-- How often reviewers accept the suggestion as is. Use this to tune the
-- auto-apply thresholds in the workflow's Settings node.
create or replace view triage.review_outcomes as
select route,
       count(*)                                        as total,
       count(*) filter (where status = 'approved')     as approved_as_is,
       count(*) filter (where status = 'corrected')    as corrected,
       count(*) filter (where status = 'rejected')     as rejected,
       count(*) filter (where status = 'expired')      as expired,
       count(*) filter (where status = 'auto_applied') as auto_applied
from triage.issue_triage
where route is not null
group by route;
