from .base import Migration


class RLSPolicies(Migration):
    version = "002"
    description = "Create Row Level Security policies for all tables"

    def up(self, api):
        policies = [
            # Users
            """CREATE POLICY "users_select_own" ON public.users
                FOR SELECT USING (auth.uid() = id)""",
            """CREATE POLICY "users_update_own" ON public.users
                FOR UPDATE USING (auth.uid() = id)""",
            # Conversations
            """CREATE POLICY "conversations_select_own" ON public.conversations
                FOR SELECT USING (auth.uid() = user_id)""",
            """CREATE POLICY "conversations_insert_own" ON public.conversations
                FOR INSERT WITH CHECK (auth.uid() = user_id)""",
            """CREATE POLICY "conversations_update_own" ON public.conversations
                FOR UPDATE USING (auth.uid() = user_id)""",
            """CREATE POLICY "conversations_delete_own" ON public.conversations
                FOR DELETE USING (auth.uid() = user_id)""",
            # Messages
            """CREATE POLICY "messages_select_own" ON public.messages
                FOR SELECT USING (
                    EXISTS (SELECT 1 FROM public.conversations WHERE id = conversation_id AND user_id = auth.uid())
                )""",
            """CREATE POLICY "messages_insert_own" ON public.messages
                FOR INSERT WITH CHECK (
                    EXISTS (SELECT 1 FROM public.conversations WHERE id = conversation_id AND user_id = auth.uid())
                )""",
            # System logs
            """CREATE POLICY "system_logs_insert_service" ON public.system_logs
                FOR INSERT WITH CHECK (true)""",
            # Agent state
            """CREATE POLICY "agent_state_select_all" ON public.agent_state
                FOR SELECT USING (true)""",
            """CREATE POLICY "agent_state_insert_service" ON public.agent_state
                FOR INSERT WITH CHECK (true)""",
            """CREATE POLICY "agent_state_update_service" ON public.agent_state
                FOR UPDATE USING (true)""",
        ]
        for sql in policies:
            api.run(sql)

    def down(self, api):
        tables = ["users", "conversations", "messages", "system_logs", "agent_state"]
        policy_names = [
            "users_select_own", "users_update_own",
            "conversations_select_own", "conversations_insert_own",
            "conversations_update_own", "conversations_delete_own",
            "messages_select_own", "messages_insert_own",
            "system_logs_insert_service",
            "agent_state_select_all", "agent_state_insert_service", "agent_state_update_service",
        ]
        for table in tables:
            for policy in policy_names:
                api.run(f'DROP POLICY IF EXISTS "{policy}" ON public.{table}')
