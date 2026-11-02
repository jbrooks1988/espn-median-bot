python
import json
import urllib.request
import urllib.error
from workers import WorkerEntrypoint

class MyWorker(WorkerEntrypoint):
    async def scheduled(self, event):
        """
        Main entry point triggered automatically on your Cloudflare Cron Schedule.
        """
        # Pass the background worker context properties directly into your main logic
        await on_scheduled(event, self.env, self.ctx)

def calculate_median(scores):
    """Calculates the median from a list of numerical team scores."""
    if not scores:
        return 0.0
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_scores[mid - 1] + sorted_scores[mid]) / 2.0
    return sorted_scores[mid]

async def on_scheduled(event, env, ctx):
    # 1. Fetch encrypted parameters securely from the environment vault
    league_id = env.get("LEAGUE_ID")
    webhook_url = env.get("DISCORD_WEBHOOK_URL")
    espn_s2 = env.get("ESPN_S2")
    swid = env.get("SWID")
    kv_store = env.get("MEDIAN_STORE")

    # Connects to ESPN API v3 Matchup Score endpoint
    espn_url = f"https://espn.com{league_id}?view=mMatchupScore"

    req = urllib.request.Request(espn_url)
    cookie_str = f"espn_s2={espn_s2}; swid={swid};"
    req.add_header("Cookie", cookie_str)
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        # Request live layout data from ESPN
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())

        teams = data.get("teams", [])
        team_data = []
        live_scores = []
        projected_scores = []

        # Parse live scoring metrics and running real-time projections
        for team in teams:
            team_id = team.get("id")
            location = team.get("location", "Team")
            nickname = team.get("nickname", f"Team {team_id}")
            name = f"{location} {nickname}".strip()

            live_score = float(team.get("record", {}).get("overall", {}).get("pointsFor", 0.0))
            if "roster" in team and "appliedStatTotal" in team["roster"]:
                live_score = float(team["roster"]["appliedStatTotal"])

            projected_score = live_score
            schedule = data.get("schedule", [])
            for matchup in schedule:
                if matchup.get("away", {}).get("teamId") == team_id:
                    projected_score = float(matchup.get("away", {}).get("totalProjectedPointsLive", live_score))
                    break
                elif matchup.get("home", {}).get("teamId") == team_id:
                    projected_score = float(matchup.get("home", {}).get("totalProjectedPointsLive", live_score))
                    break

            team_data.append({"name": name, "live": live_score, "proj": projected_score})
            live_scores.append(live_score)
            projected_scores.append(projected_score)

        if not live_scores:
            return

        # Calculate math metrics
        live_median = calculate_median(live_scores)
        proj_median = calculate_median(projected_scores)

        # Sort leaderboard list from highest live score to lowest
        team_data = sorted(team_data, key=lambda x: x["live"], reverse=True)

        # 2. Retrieve chronological season records from KV Storage bucket
        history_string = await kv_store.get("MEDIAN_HISTORY")
        if not history_string:
            history_string = "None recorded yet."

        # 3. Build the visual markdown tables
        above_median_list = []
        below_median_list = []

        for team in team_data:
            status = "🟢 ABOVE" if team["live"] >= live_median else "🔴 BELOW"
            row = f"| {team['name'][:14]:<14} | {team['live']:>5.1f} | {team['proj']:>5.1f} | {status} |"
            if team["live"] >= live_median:
                above_median_list.append(row)
            else:
                below_median_list.append(row)

        header = f"📊 **LIVE LEAGUE MEDIAN RADAR** 📊\n"
        header += f"**Current Live Median:** **{live_median:.2f}** | [ Estimated Final Median: **{proj_median:.2f}** ]\n"
        header += "```md\n"
        header += f"| {'Team Name':<14} | {'Live':>5} | {'Proj':>5} | Status vs Live |\n"
        header += f"| {'-'*14} | {'-'*5} | {'-'*5} | {'-'*14} |\n"

        table_content = "\n".join(above_median_list)
        table_content += f"\n| {'-'*14} | {'-'*5} | {'-'*5} | [LIVE MEDIAN TARGET: {live_median:.1f}] |\n"
        table_content += "\n".join(below_median_list)

        footer = "\n```\n"
        footer += f"📅 **Historical Season Medians:**\n{history_string}\n"
        footer += "\n*Last updated automatically via Cloudflare Serverless Integration.*"

        discord_message = header + table_content + footer

        # 4. Check for an active tracking ID inside KV cache to perform message edits
        last_msg_id = await kv_store.get("LAST_MESSAGE_ID")
        payload = json.dumps({"content": discord_message}).encode("utf-8")

        target_url = f"{webhook_url}?wait=true"
        method = "POST"

        if last_msg_id:
            target_url = f"{webhook_url}/messages/{last_msg_id}"
            method = "PATCH"

        disc_req = urllib.request.Request(
            target_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method=method
        )

        try:
            with urllib.request.urlopen(disc_req) as disc_res:
                res_data = json.loads(disc_res.read().decode())
                if method == "POST" and "id" in res_data:
                    await kv_store.put("LAST_MESSAGE_ID", res_data["id"])
                    print(f"Tracking established on message ID: {res_data['id']}")
                else:
                    print("Successfully updated existing pinned message.")
        except urllib.error.HTTPError as he:
            if he.code == 404 and method == "PATCH":
                print("Tracked interface missing on Discord. Wiping cache...")
                await kv_store.delete("LAST_MESSAGE_ID")
        except Exception as e:
            print(f"Execution error bypassed: {str(e)}")

    except Exception as e:
        print(f"Data pull or calculations failed: {str(e)}")
