-- ================================================================
-- RLS — Retrogaming CRM
-- Ejecutar en: Supabase Dashboard → SQL Editor
-- ================================================================
--
-- CÓMO FUNCIONA:
--   · anon key sin sesión    → RLS bloquea todo (acceso denegado)
--   · anon key + JWT válido  → rol 'authenticated' → pasan las policies
--   · service_role key (n8n) → RLS se ignora completamente (bypass nativo)
--
-- RESULTADO: n8n sigue funcionando sin ningún cambio.
-- ================================================================


-- ----------------------------------------------------------------
-- 0. Pre-checks: estado actual de RLS
-- ----------------------------------------------------------------
-- (Opcional: ejecuta esto primero para ver el estado antes del cambio)
--
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
--   AND tablename IN ('conversaciones', 'clientes');


-- ----------------------------------------------------------------
-- 1. Habilitar RLS en ambas tablas
-- ----------------------------------------------------------------
ALTER TABLE public.conversaciones ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.clientes       ENABLE ROW LEVEL SECURITY;


-- ----------------------------------------------------------------
-- 2. Policies — tabla: conversaciones
-- ----------------------------------------------------------------

-- SELECT: leer mensajes
CREATE POLICY "conversaciones_select"
  ON public.conversaciones
  FOR SELECT
  TO authenticated
  USING (true);

-- INSERT: guardar mensajes nuevos (CRM + n8n vía proxy)
CREATE POLICY "conversaciones_insert"
  ON public.conversaciones
  FOR INSERT
  TO authenticated
  WITH CHECK (true);

-- UPDATE: marcar como leído, editar campos
CREATE POLICY "conversaciones_update"
  ON public.conversaciones
  FOR UPDATE
  TO authenticated
  USING (true)
  WITH CHECK (true);

-- DELETE: eliminar mensajes (si aplica)
CREATE POLICY "conversaciones_delete"
  ON public.conversaciones
  FOR DELETE
  TO authenticated
  USING (true);


-- ----------------------------------------------------------------
-- 3. Policies — tabla: clientes
-- ----------------------------------------------------------------

-- SELECT: leer datos de clientes, etapas del pipeline
CREATE POLICY "clientes_select"
  ON public.clientes
  FOR SELECT
  TO authenticated
  USING (true);

-- INSERT: crear cliente nuevo
CREATE POLICY "clientes_insert"
  ON public.clientes
  FOR INSERT
  TO authenticated
  WITH CHECK (true);

-- UPDATE: cambiar modo (agente/humano), etapa del pipeline, nombre
CREATE POLICY "clientes_update"
  ON public.clientes
  FOR UPDATE
  TO authenticated
  USING (true)
  WITH CHECK (true);

-- DELETE: eliminar cliente
CREATE POLICY "clientes_delete"
  ON public.clientes
  FOR DELETE
  TO authenticated
  USING (true);


-- ----------------------------------------------------------------
-- 4. Post-checks: verificar que todo quedó bien
-- ----------------------------------------------------------------
-- (Ejecuta esto después para confirmar el resultado)
--
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
--   AND tablename IN ('conversaciones', 'clientes');
--
-- SELECT tablename, policyname, cmd, roles
-- FROM pg_policies
-- WHERE schemaname = 'public'
--   AND tablename IN ('conversaciones', 'clientes')
-- ORDER BY tablename, cmd;
