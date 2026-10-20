// Copy to web/config.js and fill in from Supabase → Project Settings → API.
// The anon key is a public key: access is enforced by row-level security and the email whitelist.
window.CLAB_CONFIG = {
  supabaseUrl: "https://YOUR-PROJECT-REF.supabase.co",
  supabaseAnonKey: "YOUR-ANON-PUBLIC-KEY",
};
