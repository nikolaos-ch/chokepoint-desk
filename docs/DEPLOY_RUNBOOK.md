# Deploy + refresh runbook (#5)

Flow: GitHub Action (daily 06:30 UTC + Tue after PortWatch 9 AM ET publish) -> scripts/refresh.sh (pipeline, then forecast) -> commit data/ only if changed -> push -> Vercel Git integration redeploys.

Failure safety: refresh.sh snapshots data/ and restores it on any non-zero exit; the job then fails, nothing is committed, the live site keeps the last good data. Check the Actions tab for red runs.

Manual run: Actions > "Refresh data" > Run workflow (or `gh workflow run refresh-data.yml`).

Go-live needs (user): see check-in. Cost: GitHub Actions free for public repos (private: ~1-2 min/day of 2000 free min); Vercel Hobby free, non-commercial use only (flag if the Desk becomes paid).
Rollback: Vercel dashboard > Deployments > Promote previous; or `git revert` the bad data commit.
