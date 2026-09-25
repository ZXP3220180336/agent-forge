-- 客户端默认值由 ORM 提供；直接 SQL 不隐式填充时间、文本或 JSON。
CREATE TABLE public.sessions (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    title VARCHAR(200),
    system_prompt TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20),
    meta JSON
);
CREATE INDEX ix_sessions_user_id ON public.sessions (user_id);

CREATE TABLE public.messages (
    id BIGSERIAL NOT NULL PRIMARY KEY,
    session_id VARCHAR(36) REFERENCES public.sessions (id),
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    reasoning_content TEXT,
    token_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE,
    meta JSON
);
CREATE INDEX ix_messages_session_id ON public.messages (session_id);
