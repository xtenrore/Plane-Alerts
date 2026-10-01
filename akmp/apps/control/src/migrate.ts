import { migrate, pool } from "./db.js";
await migrate();
await pool.end();
console.log("A.K.M.P database migrations complete");
