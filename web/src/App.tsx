import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AdminFeedbackPage } from "./pages/AdminFeedbackPage";
import { AdminPage } from "./pages/AdminPage";
import { AdminUserPage } from "./pages/AdminUserPage";
import { LoginPage } from "./pages/LoginPage";
import { ProfilePage } from "./pages/ProfilePage";
import { ThreadPage } from "./pages/ThreadPage";
import { TrashPage } from "./pages/TrashPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="*"
          element={
            <Layout>
              <Routes>
                <Route path="/" element={<ThreadPage />} />
                <Route path="/threads/:threadId" element={<ThreadPage />} />
                <Route path="/profile" element={<ProfilePage />} />
                <Route path="/admin" element={<AdminPage />} />
                <Route path="/admin/users/:userId" element={<AdminUserPage />} />
                <Route path="/admin/feedback/:feedbackId" element={<AdminFeedbackPage />} />
                <Route path="/trash" element={<TrashPage />} />
              </Routes>
            </Layout>
          }
        />
      </Routes>
    </QueryClientProvider>
  );
}
