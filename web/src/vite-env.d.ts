/// <reference types="vite/client" />

declare module '*.kql?raw' {
  const content: string;
  export default content;
}
