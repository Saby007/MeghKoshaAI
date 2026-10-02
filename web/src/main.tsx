import React from 'react';
import ReactDOM from 'react-dom/client';
import './App.css';
import './report-workspace.css';
import './accessible-type.css';
import './polish.css';
import { installTruncationTitles } from './presentation';
import App from './App';

installTruncationTitles();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
