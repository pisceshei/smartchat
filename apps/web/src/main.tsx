import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
import zhTW from "antd/locale/zh_TW";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { antdTheme, injectCssVars } from "./theme/tokens";
import "./styles/global.css";

injectCssVars();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ConfigProvider theme={antdTheme} locale={zhTW}>
        <AntApp>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
