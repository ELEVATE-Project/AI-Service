# llm-service

Run the docs locally to get started:

```bash
mkdocs serve -a 127.0.0.1:8001 --livereload
```

Then open [http://127.0.0.1:8001](http://127.0.0.1:8001) for setup instructions and API usage.

---

## Git Workflow — Working on the Right Branch

Before writing any code, make sure you are branching off the correct base branch.

### Step 1: Check your remote

```bash
git remote -v
```

This shows what remote repositories you are pointing to. If you do not see the Elevate GitHub repository listed, add it as a remote named `elevate`:

```bash
git remote add elevate https://github.com/<org>/<repo>.git
```

Replace `<org>/<repo>` with the actual repository path. Then verify it was added:

```bash
git remote -v
```

### Step 2: Fetch all remote branches

```bash
git fetch --all
```

This updates your local knowledge of all remote branches without changing your working directory.

### Step 3: List remote branches to find the latest official branch

```bash
git branch -r
```

Look for the latest release branch (e.g. `origin/release-1.0.0`) or whatever branch the team is currently working from. Confirm with your team if unsure.

### Step 4: Create your local branch from the official branch

It is better to branch off the upstream remote (e.g. `elevate`) rather than `origin`, so your base is always the canonical source of truth. Check `git remote -v` to confirm which remote name points to the upstream repo.

```bash
git checkout -b your-feature-branch elevate/release-1.0.0
```

If your upstream remote is named differently (e.g. `origin`), replace `elevate` with that name:

```bash
git checkout -b your-feature-branch origin/release-1.0.0
```

Replace `release-1.0.0` with the actual latest branch name if it differs. Confirm with your team which branch is currently active.

> **Note: After raising a PR to elevate, make sure to do a CodeRabbit review.**