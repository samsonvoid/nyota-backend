from .base import Migration


class AutoCleanup(Migration):
    version = "003"
    description = "Add auto-cleanup trigger for old logs and conversation archival function"

    def up(self, api):
        # Function to archive conversations older than 30 days
        api.run("""
            CREATE OR REPLACE FUNCTION public.archive_old_conversations()
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            AS $$
            BEGIN
                UPDATE public.conversations
                SET status = 'archived'
                WHERE status = 'active'
                  AND updated_at < now() - INTERVAL '30 days';
            END;
            $$;
        """)

        # Function to delete logs older than 90 days
        api.run("""
            CREATE OR REPLACE FUNCTION public.cleanup_old_logs()
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            AS $$
            BEGIN
                DELETE FROM public.system_logs
                WHERE created_at < now() - INTERVAL '90 days';
            END;
            $$;
        """)

        # Auto-run cleanup daily via pg_cron (if available)
        api.run("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                    PERFORM cron.schedule('nyota-cleanup-logs', '0 3 * * *',
                        'SELECT public.cleanup_old_logs()');
                    PERFORM cron.schedule('nyota-archive-conversations', '0 4 * * *',
                        'SELECT public.archive_old_conversations()');
                END IF;
            END;
            $$;
        """)

    def down(self, api):
        api.run("DROP FUNCTION IF EXISTS public.archive_old_conversations()")
        api.run("DROP FUNCTION IF EXISTS public.cleanup_old_logs()")
