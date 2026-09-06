import { Route, Routes } from 'react-router'

import { RedirectIfAuthenticated, RequireAuth } from '@/features/auth/RouteGuards'
import { ChatPage } from '@/routes/ChatPage'
import { ForgotPasswordPage } from '@/routes/ForgotPasswordPage'
import { LandingPage } from '@/routes/LandingPage'
import { LoginPage } from '@/routes/LoginPage'
import { NotFoundPage } from '@/routes/NotFoundPage'
import { RegisterPage } from '@/routes/RegisterPage'
import { ResetPasswordPage } from '@/routes/ResetPasswordPage'

/**
 * Route table.
 *
 * Three groups: public, anonymous-only (signing in again when you already have
 * a session is a dead end, so those redirect), and guarded. The catch-all
 * always stays last.
 *
 * `/reset-password` is public rather than anonymous-only on purpose: someone
 * with a stale session in one tab must still be able to complete a reset from
 * an emailed link.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />

      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      </Route>

      <Route element={<RequireAuth />}>
        <Route path="/chat" element={<ChatPage />} />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
