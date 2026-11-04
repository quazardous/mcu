import { mount } from "svelte";
import "./app.css";
import "uplot/dist/uPlot.min.css";
import App from "./App.svelte";

mount(App, { target: document.getElementById("app") });
