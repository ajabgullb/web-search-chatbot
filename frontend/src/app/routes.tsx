import { Route, Routes } from 'react-router'

import { RedirectIfAuthenticated, RequireAuth } from '@/features/auth/RouteGuards'
import { ChatLayout } from '@/features/chat/ChatLayout'
import { ChatPage } from '@/routes/ChatPage'
import { ConversationPage } from '@/routes/ConversationPage'
import { ForgotPasswordPage } from '@/routes/ForgotPasswordPage'
import { LandingPage } from '@/routes/LandingPage'
import { LoginPage } from '@/routes/LoginPage'
import { NotFoundPage } from '@/routes/NotFoundPage'
import { RegisterPage } from '@/routes/RegisterPage'
import { ResetPasswordPage } from '@/routes/ResetPasswordPage'
import { VerifyEmailPage } from '@/routes/VerifyEmailPage'

/**
 * Route table.
 *
 * Three groups: public, anonymous-only (signing in again when you already have
 * a session is a dead end, so those redirect), and guarded. The catch-all
 * always stays last.
 *
 * `/reset-password` and `/verify-email` are public rather than anonymous-only
 * on purpose: an emailed link must work whatever session the browser holds.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route path="/verify-email" element={<VerifyEmailPage />} />

      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      </Route>

      <Route element={<RequireAuth />}>
        {/* One layout for both, so switching threads keeps the sidebar mounted. */}
        <Route element={<ChatLayout />}>
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ConversationPage />} />
        </Route>
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
