def render_status_line(status):
    print(
        f"Angel: {status.get('angel')} | "
        f"Jugaad: {status.get('jugaad')} | "
        f"Database: {status.get('database')} | "
        f"Last Data: {status.get('last_data')} | "
        f"Next Cycle: {status.get('next_cycle')}"
    )
