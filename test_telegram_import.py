import sys
sys.path.insert(0, '.')

# Test imports
try:
    from app.notifications.telegram import TelegramNotifier, send_recommendation, get_telegram_notifier
    print('Telegram module imports successfully')
    
    # Test that Telegram is disabled by default (no credentials)
    notifier = TelegramNotifier()
    print(f'Telegram notifier created, enabled={notifier.enabled}')
    
    # Test convenience functions
    notif = get_telegram_notifier()
    print(f'Global notifier retrieved, enabled={notif.enabled}')
    
except Exception as e:
    print(f'Import failed: {e}')
    import traceback
    traceback.print_exc()