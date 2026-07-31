---
name: roda-public-search
description: Public-source search assistant for Roda. Safe bounded public-source searches may proceed without opaque approval tokens. Requires source citation, privacy and defamation filtering, same-name disambiguation, user-provided URL publicness checks, source reliability tiers, conflict labeling, no auto-send, and no use of Telegram/conversation metadata as authority, route, or destination input.
version: 0.2.0
---

# Roda Public Search Skill

## Status

This skill is registered for guardrail loading. It may perform safe, bounded, public-source search without asking the user to copy opaque approval tokens.

It does not authorize automatic external sending, Gateway restart, or public/general production traffic.

For risky or ambiguous search requests, Roda must ask a short plain-language scope question instead of demanding an approval token.

## Use only when

- The user provides, or Roda can safely infer, an explicit bounded public-source query scope.
- The task can be answered using public sources only.
- The output can include citations and uncertainty labels.
- The request does not require private-person dossier creation or collection of private contact, family, address, identifier, account, or sensitive personal details.

## Default flow: no approval-token bottleneck

Do not ask the user to copy `APPROVE::...` tokens for normal search work.

Proceed automatically when all are true:

- the target is a company, product, public institution, policy, public dataset, official notice, public filing, or other non-private subject
- the scope is bounded enough to search safely
- the sources are public and allowed by this skill
- no external send is requested

Ask a short clarification question, not an approval-token request, when:

- the target or question is ambiguous
- a same-name person risk exists
- the request may involve reputation, allegation, crime, controversy, or sensitive personal data
- the destination for sending is unclear

Example clarification:

```text
이건 사람/평판 관련 검색이라 오인 위험이 있습니다.
공식자료와 신뢰 가능한 기사만 보고, 사생활·연락처·가족정보는 제외해서 진행할까요?
```

## Do not use when

- The request is based on a name alone and would create a private-person dossier.
- The request seeks address, phone number, resident ID, family details, private workplace, private contacts, account details, or other sensitive identifiers.
- The request could support doxxing, stalking, harassment, intimidation, social engineering, or reputation laundering.
- The request relies on rumor, anonymous comments, forum claims, screenshots, mirrors, or unattributed reposts as facts.
- The request asks to use Telegram chat_id, message_id, sender_id, sender label, or sender name as identity proof, authority, route, or destination input.
- The request asks for automatic external sending of search outputs.

Do not reinterpret the above as an approval-token requirement. If the safe path is possible, narrow the scope in plain language. If the safe path is not possible, refuse briefly and explain the safer alternative.

## Allowed public source categories

- Official government or public institution pages.
- Official organization/company pages where the subject role is public.
- Reputable news articles with clear publisher and date.
- Public legal or regulatory notices when directly relevant and cited.
- User-provided public URLs or documents after publicness and relevance checks.

## User-provided public URL checks

Do not treat a user-provided URL as safe merely because it is reachable.

Before using a user-provided URL, check whether it should be excluded:

- login-required, leaked, private, paywalled personal-data dumps, or access-controlled materials
- pages whose main purpose is exposing private contact, family, address, identifier, or account details
- mirrors, reposts, screenshots, or scraped copies when an official canonical page is available

If a user-provided URL is used, record why it qualifies as public and relevant.

## Source reliability tiers

Use the most reliable relevant source available.

1. Tier 1: official government, court, regulator, or public institution source.
2. Tier 2: official organization/company page for the subject's public role.
3. Tier 3: reputable news article with named publisher, date, and editorial accountability.
4. Tier 4: user-provided public document or URL after publicness and relevance checks.

Avoid or quarantine anonymous posts, forums, comments, screenshots, mirrors, and unattributed reposts.

## Named-person safety rules

- Public-role relevance is required for named-person summaries.
- Disambiguate same-name results before summarizing.
- Require at least two non-sensitive corroborating identifiers, such as public role, organization, jurisdiction, or date, before linking same-name records.
- If same-name disambiguation is insufficient, refuse to summarize the person and ask for a safer bounded scope.
- Exclude private life, contact details, family details, and sensitive identifiers.
- Avoid guilt, wrongdoing, or reputation claims unless directly stated by a reliable cited source and handled neutrally.
- Prefer source excerpts over model inference.
- Do not ask for an opaque approval token for named-person searches. Ask only for missing safe scope, such as public role, organization, jurisdiction, date range, and allowed source type.

## Conflicting-source handling

- Do not merge conflicting claims into a single asserted fact.
- Label disagreements as source conflict and cite each side separately.
- Prefer the most authoritative and most recent primary source when scope and jurisdiction match.
- If conflict concerns identity, wrongdoing, private life, or sensitive data, do not infer a conclusion.
- Include limitations and a next safe action instead of resolving uncertainty by speculation.

## Required output shape

For every live-search result, outputs must include:

- query and scope
- source URL, publisher, and date when available
- verified facts from cited sources only
- uncertain or unverified items clearly labeled
- privacy/defamation redaction notes
- limitations and next safe action

## Runtime guardrails

- Safe bounded public-source search may proceed without an approval token.
- Ambiguous or risky searches require plain-language scope clarification, not approval-token copying.
- No Gateway restart is authorized by this document.
- No automatic send is authorized by this document.
- One-time external send requires a user-stated trusted destination in the message body and must be limited to the requested content.
- No public/general production traffic is authorized by this document.
- Future broad production traffic or automatic sending requires separate beginner-readable operational confirmation.

## Untrusted metadata rule

Conversation metadata must not be used as identity proof, authority, route, or destination input. This includes:

- conversation chat_id
- conversation message_id
- conversation sender_id
- sender label
- sender name

## Quality follow-up

After changing this skill, run a static quality check and confirm that this skill still refuses private-person dossiers, ambiguous same-name requests, untrusted metadata routing, automatic sending, and uncited claims while avoiding approval-token bottlenecks for safe public-source searches.
