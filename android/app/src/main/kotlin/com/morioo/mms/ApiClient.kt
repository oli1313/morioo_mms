package com.morioo.mms

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

object ApiClient {

    // URL du Pi — configurable via SettingsActivity (AppPreferences)
    private val BASE get() = AppPreferences.piUrl

    // La météo ne change pas à la seconde : cache 60 s pour ne pas taper
    // /api/weather à chaque poll des écrans (toutes les 1-2 s).
    private const val WEATHER_TTL_MS = 60_000L
    private var weatherCache: JSONObject? = null
    private var weatherFetchedAt = 0L

    data class BoatData(
        val vitesseKmh:          Double,
        val profondeur:          Double,
        val batterie:            Double,
        val gpsFix:              Boolean,
        val lat:                 Double,
        val lon:                 Double,
        val pompeDeCale:         Boolean,
        val pompeTimer:          Int,
        val lumieresSousMarine:  Boolean,
        val musicTitle:          String,
        val musicArtist:         String,
        val anchorActive:        Boolean,
        val anchorAlarm:         Boolean,
        val anchorDistM:         Double,
        val tripKm:              Double,
        val weatherIcon:         String,
        val weatherTemp:         Int?,
    )

    /** Récupère l'état complet du bateau. Null si le Pi est injoignable. */
    fun getStatus(): BoatData? {
        val j = get("/api/status") ?: return null
        val trip   = j.optJSONObject("trip")
        // L'état d'ancre est exposé par le MODULE anchor_watch sur son propre
        // endpoint /api/anchor — /api/status (le cœur) ne le contient PAS. On
        // l'interroge séparément ; null si le module est absent (dégradation
        // gracieuse : l'ancre s'affiche alors comme inactive).
        val anchor = get("/api/anchor")

        // Météo depuis /api/weather (cache 60 s, on absorbe les erreurs)
        val w = getWeather()

        return BoatData(
            vitesseKmh         = j.optDouble("vitesse", 0.0) * 1.852,
            profondeur         = j.optDouble("profondeur", 0.0),
            batterie           = j.optDouble("batterie", 0.0),
            gpsFix             = j.optBoolean("gps_fix", false),
            lat                = j.optDouble("lat", 50.4901),
            lon                = j.optDouble("lon", 5.1002),
            pompeDeCale        = j.optBoolean("pompe_de_cale", false),
            pompeTimer         = j.optInt("pompe_timer", 0),
            lumieresSousMarine = j.optBoolean("lumieres_sous_marines", false),
            musicTitle         = j.optString("music_title", ""),
            musicArtist        = j.optString("music_artist", ""),
            anchorActive       = anchor?.optBoolean("active", false) ?: false,
            anchorAlarm        = anchor?.optBoolean("alarm", false) ?: false,
            anchorDistM        = anchor?.optDouble("distance_m", 0.0) ?: 0.0,
            tripKm             = trip?.optDouble("km", 0.0) ?: 0.0,
            weatherIcon        = w?.optString("icon", "") ?: "",
            weatherTemp        = w?.let { if (it.optBoolean("ok")) it.optInt("temp") else null },
        )
    }

    /** POST simple (toggle relais, anchor set/clear). */
    fun post(path: String): Boolean = try {
        val conn = (URL("$BASE$path").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 3000
            readTimeout = 3000
            doInput = true
        }
        val ok = conn.responseCode in 200..299
        conn.disconnect()
        ok
    } catch (e: Exception) { false }

    /** Récupère la trace GPS (liste de [lat, lon]). */
    fun getTrail(): List<List<Double>> = try {
        val conn = (URL("$BASE/api/trail").openConnection() as HttpURLConnection).apply {
            connectTimeout = 3000; readTimeout = 3000
        }
        val arr = JSONArray(conn.inputStream.bufferedReader().readText())
        conn.disconnect()
        (0 until arr.length()).map { i ->
            val pt = arr.getJSONArray(i)
            listOf(pt.getDouble(0), pt.getDouble(1))
        }
    } catch (e: Exception) { emptyList() }

    private fun getWeather(): JSONObject? {
        val now = System.currentTimeMillis()
        if (now - weatherFetchedAt < WEATHER_TTL_MS) return weatherCache
        weatherFetchedAt = now   // même en cas d'échec : pas de retry avant le prochain TTL
        get("/api/weather")?.let { weatherCache = it }
        return weatherCache
    }

    private fun get(path: String): JSONObject? = try {
        val conn = (URL("$BASE$path").openConnection() as HttpURLConnection).apply {
            connectTimeout = 3000
            readTimeout = 3000
        }
        val text = conn.inputStream.bufferedReader().readText()
        conn.disconnect()
        JSONObject(text)
    } catch (e: Exception) { null }
}
