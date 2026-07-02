"""
Dynasty Report Cards — Streamlit frontend.
Roster overview -> player card view -> trade section.
"""

import os, sys, asyncio, json
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

st.set_page_config(
    page_title="Dynasty Report Cards",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _init_secrets():
    """Sync Streamlit Cloud secrets into os.environ (no-op locally, .env already loaded)."""
    for key in ["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "SLEEPER_LEAGUE_ID", "ROSTER_OWNER_ID"]:
        try:
            val = str(st.secrets[key]).strip()
            if val:
                os.environ[key] = val
        except Exception:
            pass


_init_secrets()

from data import sleeper_client
from agents.synthesis_agent import run_synthesis_agent
from agents.roster_agent import run_roster_agent
from agents.trade_agent import run_trade_agent

LEAGUE_ID = os.getenv("SLEEPER_LEAGUE_ID")
OWNER = os.getenv("ROSTER_OWNER_ID", "BCNH")

GRADE_COLORS = {"A": "#3fb950", "B": "#3b82f6", "C": "#e8b84b", "D": "#e3873c", "F": "#f85149"}


def grade_color(grade: str) -> str:
    if not grade:
        return "#9aa6bb"
    return GRADE_COLORS.get(grade[0].upper(), "#9aa6bb")


def grade_pill(grade: str) -> str:
    c = grade_color(grade)
    return (
        f'<span style="display:inline-block;padding:0.15rem 0.6rem;border-radius:999px;'
        f'font-weight:800;font-size:0.9rem;background:{c}22;color:{c};border:1px solid {c}66;">'
        f'{grade or "—"}</span>'
    )


# ── Session state ──────────────────────────────────────────────────────────────

for key, default in [
    ("player_cards", {}),
    ("roster_grade", None),
    ("trade_report", None),
    ("selected_player_id", None),
    ("view", "overview"),
]:
    if key not in st.session_state:
        st.session_state[key] = default


@st.cache_data(ttl=3600, show_spinner="Loading roster from Sleeper...")
def load_roster():
    roster = sleeper_client.get_roster_by_display_name(LEAGUE_ID, OWNER)
    players = sleeper_client.resolve_roster_players(roster)
    players = [p for p in players if p.get("position") in ("QB", "RB", "WR", "TE")]
    players.sort(key=lambda p: (p.get("position") or "", p.get("full_name") or ""))
    return players


players = load_roster()

st.title("📋 Dynasty Report Cards")
st.caption(f"Roster report card for **{OWNER}** — 12-team superflex PPR dynasty")

# ── Sidebar nav ───────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## Navigation")
    if st.button("🏠 Roster Overview", use_container_width=True):
        st.session_state.view = "overview"
        st.session_state.selected_player_id = None
        st.rerun()
    if st.button("💱 Trade Section", use_container_width=True):
        st.session_state.view = "trade"
        st.rerun()
    st.divider()
    st.caption(f"{len(players)} skill-position players on roster")
    st.caption(f"{len(st.session_state.player_cards)} report card(s) generated")


def _run_all_report_cards():
    progress = st.progress(0.0, text="Generating report cards...")
    for i, p in enumerate(players):
        pid = p["player_id"]
        if pid not in st.session_state.player_cards:
            progress.progress(i / len(players), text=f"Grading {p.get('full_name')}...")
            try:
                card = asyncio.run(run_synthesis_agent(pid))
                st.session_state.player_cards[pid] = card
            except Exception as e:
                st.session_state.player_cards[pid] = {
                    "error": str(e), "player": p.get("full_name"), "position": p.get("position"),
                }
    progress.progress(1.0, text="Done")
    progress.empty()


# ── Roster overview ────────────────────────────────────────────────────────────

BANNER_STYLE = (
    "background:linear-gradient(135deg, #1b2744 0%, #2c3b66 100%);"
    "border-radius:14px;padding:1.75rem 2rem;margin-bottom:1rem;"
    "border:1px solid rgba(255,255,255,0.08);"
)


def render_team_report_banner(roster_grade: dict | None, generated: int, total: int):
    """One cohesive banner that morphs from a call-to-action into the graded
    team summary, so the whole flow reads as one 'team report card' experience
    rather than a bare button plus a pile of individual player cards."""
    if roster_grade:
        grade = roster_grade.get("overall_grade", "—")
        c = grade_color(grade)
        st.markdown(
            f'''<div style="{BANNER_STYLE}">
              <div style="display:flex;align-items:center;gap:0.9rem;flex-wrap:wrap;margin-bottom:0.6rem;">
                <span style="font-size:1.5rem;font-weight:800;color:#fff;">Team Report Card</span>
                <span style="display:inline-block;padding:0.2rem 0.85rem;border-radius:999px;font-weight:800;
                  font-size:1.15rem;background:{c}22;color:{c};border:1px solid {c}66;">{grade}</span>
              </div>
              <div style="display:flex;gap:1.75rem;flex-wrap:wrap;margin-bottom:0.85rem;">
                <div><div style="font-size:0.72rem;color:#9aa6bb;text-transform:uppercase;letter-spacing:.05em;">Starter Quality</div>
                  <div style="font-size:1.1rem;font-weight:700;color:#fff;">{roster_grade.get("starter_quality_score", "—")}</div></div>
                <div><div style="font-size:0.72rem;color:#9aa6bb;text-transform:uppercase;letter-spacing:.05em;">Strongest</div>
                  <div style="font-size:1.1rem;font-weight:700;color:#fff;">{roster_grade.get("strongest_position", "—")}</div></div>
                <div><div style="font-size:0.72rem;color:#9aa6bb;text-transform:uppercase;letter-spacing:.05em;">Weakest</div>
                  <div style="font-size:1.1rem;font-weight:700;color:#fff;">{roster_grade.get("weakest_position", "—")}</div></div>
              </div>
              <div style="font-size:0.92rem;color:#dbe1f0;line-height:1.55;">{roster_grade.get("narrative", "")}</div>
            </div>''',
            unsafe_allow_html=True,
        )
        for flag in roster_grade.get("flags", []):
            st.warning(flag)
        st.caption("Player insights below — click a player for their full breakdown.")
        return

    subtext = (
        "Get an overall roster grade plus every player's opportunity, production, and "
        "trade-value evals — all in one pass."
        if generated == 0
        else f"{generated} of {total} player report cards generated so far — pick up where you left off."
    )
    st.markdown(
        f'''<div style="{BANNER_STYLE}">
          <div style="font-size:1.4rem;font-weight:800;color:#fff;margin-bottom:0.4rem;">
            📋 Generate Your Team Report Card</div>
          <div style="font-size:0.95rem;color:#c3cbe0;">{subtext}</div>
        </div>''',
        unsafe_allow_html=True,
    )
    btn_label = "🔄 Continue Generating" if generated > 0 else "🔄 Generate Team Report Card"
    if st.button(btn_label, type="primary", use_container_width=True):
        _run_all_report_cards()
        st.rerun()


if st.session_state.view == "overview":
    cards_ready = len(players) > 0 and len(st.session_state.player_cards) == len(players)

    if cards_ready and st.session_state.roster_grade is None:
        with st.spinner("Computing overall roster grade..."):
            valid_cards = [c for c in st.session_state.player_cards.values() if "error" not in c]
            try:
                st.session_state.roster_grade = asyncio.run(run_roster_agent(valid_cards))
            except Exception as e:
                st.error(f"Couldn't compute the overall roster grade: {e}")
                if st.button("🔄 Retry roster grade"):
                    st.rerun()

    render_team_report_banner(st.session_state.roster_grade, len(st.session_state.player_cards), len(players))

    st.divider()

    by_position: dict[str, list] = {}
    for p in players:
        by_position.setdefault(p.get("position"), []).append(p)

    if st.session_state.player_cards:
        st.markdown("### Player Insights")

    for pos in ("QB", "RB", "WR", "TE"):
        pos_players = by_position.get(pos, [])
        if not pos_players:
            continue
        st.markdown(f"#### {pos}")
        cols = st.columns(4)
        for i, p in enumerate(pos_players):
            pid = p["player_id"]
            card = st.session_state.player_cards.get(pid)
            with cols[i % 4]:
                with st.container(border=True):
                    st.markdown(f"**{p.get('full_name')}**")
                    st.caption(f"{p.get('team')} · Age {p.get('age', '—')}")
                    if card and "error" not in card:
                        st.markdown(
                            f"OPP {grade_pill(card.get('opportunity_grade'))} "
                            f"PROD {grade_pill(card.get('production_grade'))} "
                            f"MKT {grade_pill(card.get('trade_value_grade'))}",
                            unsafe_allow_html=True,
                        )
                    elif card and "error" in card:
                        st.caption("⚠️ Error generating report card")
                    else:
                        st.caption("Not graded yet")
                    if st.button("View →", key=f"view_{pid}", use_container_width=True):
                        st.session_state.selected_player_id = pid
                        st.session_state.view = "player"
                        st.rerun()

# ── Player card view ───────────────────────────────────────────────────────────

elif st.session_state.view == "player":
    pid = st.session_state.selected_player_id
    player = next((p for p in players if p["player_id"] == pid), None)
    if player is None:
        st.error("Player not found.")
        st.stop()

    if st.button("← Back to Roster"):
        st.session_state.view = "overview"
        st.rerun()

    st.markdown(f"## {player.get('full_name')}")
    st.caption(
        f"{player.get('position')} · {player.get('team')} · "
        f"Age {player.get('age', '—')} · Yrs Exp {player.get('years_exp', '—')}"
    )
    st.divider()

    card = st.session_state.player_cards.get(pid)
    if card is None:
        if st.button("Generate Report Card"):
            with st.spinner(f"Grading {player.get('full_name')}..."):
                try:
                    card = asyncio.run(run_synthesis_agent(pid))
                    st.session_state.player_cards[pid] = card
                except Exception as e:
                    st.error(f"Pipeline error: {e}")
                    st.stop()
            st.rerun()
        st.stop()

    if "error" in card:
        st.error(f"Report card generation failed: {card['error']}")
        st.stop()

    st.markdown(
        f"Opportunity {grade_pill(card.get('opportunity_grade'))} &nbsp;&nbsp; "
        f"Production {grade_pill(card.get('production_grade'))} &nbsp;&nbsp; "
        f"Trade Value {grade_pill(card.get('trade_value_grade'))}",
        unsafe_allow_html=True,
    )
    st.write("")

    c1, c2, c3 = st.columns(3)
    c1.metric("Opportunity Score", card.get("opportunity_score", "—"))
    c2.metric("Production Score", card.get("production_score", "—"))
    c3.metric("Trade Value Score", card.get("trade_value_score", "—"))

    st.divider()

    m1, m2, m3 = st.columns(3)
    m1.metric("Hybrid Market Value", card.get("hybrid_market_value", "—"))
    m2.metric("FantasyCalc Dynasty Value", card.get("dynasty_value", "—"))
    m3.metric("30-Day Trend", card.get("trend_30day", "—"))

    st.divider()

    risk = card.get("risk_modifier", {}) or {}
    st.markdown(
        f"**Risk Modifier:** Durability {risk.get('durability_score', '—')}/5 · "
        f"Injury chance {risk.get('injury_chance_pct', '—')}% · "
        f"Aging risk: {risk.get('aging_risk', '—')}"
    )
    if risk.get("career_window_note"):
        st.caption(risk["career_window_note"])

    st.divider()

    st.markdown("**Analysis**")
    st.markdown(card.get("narrative", ""))

    st.divider()

    up_col, risk_col = st.columns(2)
    with up_col:
        st.markdown("**Key Strengths**")
        for item in card.get("key_strengths", []):
            st.markdown(f"✅ {item}")
    with risk_col:
        st.markdown("**Key Concerns**")
        for item in card.get("key_concerns", []):
            st.markdown(f"⚠️ {item}")

# ── Trade section ──────────────────────────────────────────────────────────────

elif st.session_state.view == "trade":
    st.subheader("💱 Trade Section — Sell Candidates")
    cards_ready = len(players) > 0 and len(st.session_state.player_cards) == len(players)

    if not cards_ready:
        st.info("Generate report cards for the full roster first (from Roster Overview) before running the trade analysis.")
        if st.button("🏠 Go to Roster Overview"):
            st.session_state.view = "overview"
            st.rerun()
        st.stop()

    if st.session_state.trade_report is None:
        if st.button("🔍 Find Sell Candidates", use_container_width=True):
            with st.spinner("Analyzing roster for sell signals..."):
                valid_cards = [c for c in st.session_state.player_cards.values() if "error" not in c]
                try:
                    st.session_state.trade_report = asyncio.run(run_trade_agent(valid_cards))
                except Exception as e:
                    st.error(f"Couldn't run the trade analysis: {e}")
                    st.stop()
            st.rerun()
        st.stop()

    report = st.session_state.trade_report
    st.markdown(report.get("summary", ""))
    if report.get("positional_surplus_summary"):
        st.caption(report["positional_surplus_summary"])

    st.divider()

    candidates = report.get("sell_candidates", [])
    if not candidates:
        st.success("No sell candidates flagged on this roster right now.")
    for c in candidates:
        with st.container(border=True):
            st.markdown(f"### {c.get('player')} · {c.get('position')}")
            st.markdown(f"**{(c.get('recommendation') or '').title()}** — {', '.join(c.get('signals', []))}")
            st.markdown(c.get("why", ""))

    if st.button("🔄 Re-run analysis"):
        st.session_state.trade_report = None
        st.rerun()
