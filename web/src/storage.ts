/** localStorage, if the browser allows it. */
export function browserStorage(): Storage | null {
  try {
    return typeof localStorage.getItem === "function" ? localStorage : null;
  } catch {
    return null;
  }
}
