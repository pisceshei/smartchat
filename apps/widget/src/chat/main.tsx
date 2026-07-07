import { render } from "preact";

import { App } from "./app";
import { bootChat } from "./controller";
import "./styles.css";

bootChat();
render(<App />, document.getElementById("app")!);
