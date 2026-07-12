# Crypto seed library

`crypto_seed_library.csv` is the 140-account editorial roster used to build the initial
internal seed set. Exactly 100 rows are in the approved validation roster and 40 remain in
review. The approved roster has 80 people, 20 institutions, fixed topic quotas, and fixed
70 English / 20 Chinese / 10 bilingual buckets.

Statuses:

- `approved`: selected for the 100-account live validation roster. It becomes an approved
  member of a versioned seed set only after the seed-build job obtains a numeric X ID and
  all 100 rows pass the live profile/content checks.
- `review`: known discovery lead, but current handle or activity still needs API/manual review.

`crypto_handles.txt` contains exactly the 80 approved personal roster handles. Institution
rows and review rows remain separate. Blank `verified_at` plus `live_validation_required`
is intentional: the project never invents a numeric ID or claims a live check that did not
happen. Run `scripts/build_seed_catalog.py` after editing the curated source lists to
regenerate both files while preserving the fixed quotas.

This is a research starting point, not an endorsement or proof that an account accepts paid
partnerships. Before outreach, refresh the profile, inspect recent original content, check
conflicts and disclosure behavior, and verify any public contact point from its source URL.
