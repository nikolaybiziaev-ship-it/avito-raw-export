# Codex live validation checklist

Goal: validate Avito Raw Export against one real Avito account without changing any account data.

## Hard rules

- Do not add or call any Avito mutation endpoint.
- No send/read/delete/blacklist/webhook/listing-update operations.
- Never print, commit, or write Client Secret / access token to repository files or logs.
- Real exports must stay outside Git.
- If the API contract differs from assumptions, preserve the raw response first, then adapt discovery logic.

## Validation sequence

1. Create a local virtual environment and install the project dependencies.
2. Run `pytest -q`.
3. Launch the UI.
4. Use the provided real Client ID / Client Secret only through the UI/local environment.
5. Press “Проверить подключение” and confirm `/core/v1/accounts/self` works.
6. Run a maximum export.
7. Verify the export contains:
   - account/self raw response;
   - all five item-status list passes;
   - rating info from `/ratings/v1/info` when accessible;
   - every available review page from `/ratings/v1/reviews` and the review ID index;
   - review-linked item IDs and review images when present;
   - item details for every discovered item ID, including IDs learned from reviews/chats;
   - global chat passes for all/u2i/u2u/a2u;
   - per-item chat passes;
   - chat details;
   - message pages for every chat;
   - voice link responses and voice files when present;
   - manifest + indexes + request/error logs.
8. Compare the oldest exported conversation date with the oldest conversation visible in the Avito UI for a small manual sample.
9. Inspect errors.jsonl and retry/fix only read paths.
10. Record factual results in a private report outside the repository: an anonymous local-only profile label, item/chat/message/review/media counts, oldest/newest message timestamp, rating/reviews endpoint availability, endpoint failures, pagination behavior, and any API-contract deviations. Do not include message texts, names, phone numbers, tokens or secrets in the report.
