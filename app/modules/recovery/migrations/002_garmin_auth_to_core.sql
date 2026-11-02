-- The Garmin token moved to core_kv (app/core/garmin.py); it's re-exchanged on the next call.
DELETE FROM recovery_kv WHERE key = 'garmin_auth';
