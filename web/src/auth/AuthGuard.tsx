/**
 * WI-05: AuthGuard — conditional route wrapper.
 *
 * Renders ``children`` only when a token is present in localStorage
 * (i.e. ``isAuthenticated()`` returns ``true``). Otherwise redirects
 * to ``/login`` via ``<Navigate>``.
 */

import { Navigate } from "react-router-dom";
import { isAuthenticated } from "../api/auth";

interface AuthGuardProps {
  children: React.ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps): JSX.Element {
  if (!isAuthenticated()) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

export default AuthGuard;
