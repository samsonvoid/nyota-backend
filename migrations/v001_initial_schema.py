from .base import Migration


class InitialSchema(Migration):
    version = "001"
    description = "Create core tables: users, conversations, messages, system_logs, agent_state"

    def up(self, api):
        api.run("""
            CREATE TABLE IF NOT EXISTS public.users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE,
                created_at TIMESTAMPTZ DEFAULT now(),
                last_active TIMESTAMPTZ DEFAULT now(),
                metadata JSONB DEFAULT '{}'::jsonb
            )
        """)

        api.run("""
            CREATE TABLE IF NOT EXISTS public.conversations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID REFERENCES public.users(id) ON DELETE CASCADE,
                title TEXT DEFAULT 'New Conversation',
                status TEXT DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deleted')),
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        api.run("""
            CREATE TABLE IF NOT EXISTS public.messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID REFERENCES public.conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        api.run("""
            CREATE TABLE IF NOT EXISTS public.system_logs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                level TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error', 'debug', 'success')),
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        api.run("""
            CREATE TABLE IF NOT EXISTS public.agent_state (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('idle', 'listening', 'thinking', 'speaking')),
                payload JSONB DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON public.messages(conversation_id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON public.messages(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON public.conversations(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_system_logs_level ON public.system_logs(level)",
            "CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON public.system_logs(created_at)",
        ]:
            api.run(idx)

        for table in ["users", "conversations", "messages", "system_logs", "agent_state"]:
            api.run(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    def down(self, api):
        tables = ["agent_state", "system_logs", "messages", "conversations", "users"]
        for table in tables:
            api.run(f"DROP TABLE IF EXISTS public.{table} CASCADE")
