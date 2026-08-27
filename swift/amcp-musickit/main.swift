// amcp-musickit — MusicKit bridge for the Apple Music MCP server.
//
// It does the handful of things the Apple Events rail cannot express, and that
// would otherwise be the only reason the server needs an Apple Music API
// credential at all:
//
//   * put a catalog song or album in the user's library (Music.app's `play`
//     needs an object specifier, and an unowned catalog track has none),
//   * attach a track to an Apple-Music-origin playlist (AppleScript can only
//     edit playlists Music.app itself owns),
//   * love/dislike a catalog song,
//   * resolve ISRCs — the one query no PUBLIC Apple endpoint answers. The
//     iTunes Search API, which covers the rest of this server's catalog needs
//     with no credential, has no ISRC filter at all.
//
// Why this removes the credential problem entirely
// ------------------------------------------------
// `MusicDataRequest` performs an authenticated Apple Music API call with
// MusicKit supplying BOTH tokens itself, from this app's identity plus the
// user's consent. The identity is the Team ID and bundle id, validated
// server-side against an App ID that has MusicKit enabled — NOT a
// provisioning-profile entitlement. Requesting
// `com.apple.developer.musickit` in an entitlements plist actually breaks it:
// nothing grants that entitlement, so the kernel SIGKILLs the process at
// launch. It must be a proper .app with an Info.plist (bundle id +
// NSAppleMusicUsageDescription), signed with Developer ID. So:
//
//   * no developer token is embedded in the shipped app (nothing to extract),
//   * no `.p8` leaves the developer's machine,
//   * no token-broker service has to exist, be paid for, or be trusted,
//   * no 6-month expiry cliff — the entitlement does not expire,
//   * the Music User Token is minted and held by MusicKit, not by us.
//
// What MusicKit on macOS deliberately does NOT do (checked against the SDK):
// `SystemMusicPlayer` and `MusicLibrary.edit` are `@available(macOS,
// unavailable)`, so this cannot drive Music.app or mutate the library through
// the Swift API. The REST endpoint via `MusicDataRequest` is the way in, and
// playback stays where it already is — Apple Events to Music.app — so there is
// still exactly one player.
//
// Surface: JSON on stdout, every identifier validated before it reaches a URL,
// no filesystem access, no shell, and no network of its own beyond Apple's API.
//
//   amcp-musickit status
//   amcp-musickit authorize
//   amcp-musickit add        <catalogSongID>
//   amcp-musickit add-album  <catalogAlbumID>
//   amcp-musickit rate       <catalogSongID> love|dislike
//   amcp-musickit playlist-add <playlistID> <trackID> [songs|library-songs]
//   amcp-musickit playlist-tracks <playlistID>
//   amcp-musickit isrc       <ISRC>[,<ISRC>...]
//
// Read verbs return Apple's response body verbatim in `body` rather than
// reshaping it. The Swift side deliberately holds no policy — the Python
// caller owns interpretation, exactly as it does for the REST rail.

import Foundation
import MusicKit

struct Result: Encodable {
    var ok: Bool
    var status: String?
    var id: String?
    var httpStatus: Int?
    var error: String?
    /// Apple's response body, verbatim, for the read verbs. Never populated for
    /// writes: a caller that needs to know whether a write landed should read
    /// `httpStatus`, not scrape prose out of a body.
    var body: String?
}

func emit(_ result: Result) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(result), let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print(#"{"ok":false,"error":"could not encode result"}"#)
    }
    exit(result.ok ? 0 : 1)
}

func fail(_ message: String) -> Never { emit(Result(ok: false, error: message)) }

func describe(_ status: MusicAuthorization.Status) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

/// Catalog ids are numeric. Validated rather than trusted: this value is
/// interpolated into a URL that reaches Apple's service, and a helper that
/// accepts arbitrary text is a helper that can be aimed somewhere else.
func validCatalogID(_ raw: String) -> String? {
    // ASCII digits only: Character.isNumber is true for Unicode digits such as
    // Arabic-Indic "٣٤٥", which must not reach the URL.
    guard !raw.isEmpty, raw.count <= 32,
          raw.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
    return raw
}

/// Apple's dot-prefixed ids: `p.AbCdEf123` for a library playlist, `i.AbCdEf123`
/// for a library song. Same reasoning as `validCatalogID`, but stricter about
/// why: these are interpolated into a URL path, where a `/` or a `..` would
/// change WHICH resource is addressed rather than merely failing.
func validPrefixedID(_ raw: String, prefix: String) -> String? {
    guard raw.count > prefix.count, raw.count <= 64, raw.hasPrefix(prefix) else { return nil }
    let tail = raw.dropFirst(prefix.count)
    guard !tail.isEmpty, tail.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) })
    else { return nil }
    return raw
}

func validPlaylistID(_ raw: String) -> String? { validPrefixedID(raw, prefix: "p.") }

/// The two things a library playlist accepts as a track. A CATALOG song
/// (`songs` + numeric id) is added to the library implicitly by the attach; a
/// track already in the library is referenced by its `library-songs` id.
func validTrackRef(_ raw: String, kind: String) -> String? {
    switch kind {
    case "songs": return validCatalogID(raw)
    case "library-songs": return validPrefixedID(raw, prefix: "i.")
    default: return nil
    }
}

/// An ISRC is 12 characters: 2-letter country, 3-char registrant, 2-digit year,
/// 5-digit designation. Accepted as a comma-separated list because Apple's
/// filter takes one — batching is the whole reason this verb is affordable.
func validISRCList(_ raw: String) -> String? {
    let codes = raw.split(separator: ",", omittingEmptySubsequences: true).map {
        $0.trimmingCharacters(in: .whitespaces).uppercased()
    }
    guard !codes.isEmpty, codes.count <= 100 else { return nil }
    for code in codes {
        guard code.count == 12,
              code.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) })
        else { return nil }
    }
    return codes.joined(separator: ",")
}

/// Every verb below this line needs consent. Factored out so a new verb cannot
/// forget it — the failure mode of forgetting is a silent unauthorized call,
/// not a compile error.
func requireAuthorization() -> Never? {
    let status = MusicAuthorization.currentStatus
    guard status == .authorized else {
        // Never prompt implicitly: a permission dialog must not appear in the
        // middle of an unrelated request.
        emit(Result(ok: false, status: describe(status),
                    error: "not authorized — run `authorize` first"))
    }
    return nil
}

/// Perform one signed Apple Music API call. MusicKit supplies both tokens from
/// the app's identity; we never see or store either.
func send(_ request: URLRequest, id: String? = nil, wantBody: Bool = false) async -> Never {
    do {
        let response = try await MusicDataRequest(urlRequest: request).response()
        let code = response.urlResponse.statusCode
        let ok = (200...299).contains(code)
        emit(Result(ok: ok, id: id, httpStatus: code,
                    error: ok ? nil : "Apple returned \(code)",
                    body: (ok && wantBody) ? String(data: response.data, encoding: .utf8) : nil))
    } catch {
        fail(error.localizedDescription)
    }
}

func cmdStatus() -> Never {
    emit(Result(ok: true, status: describe(MusicAuthorization.currentStatus)))
}

func cmdAuthorize() async -> Never {
    // Native prompt attributed to THIS app — no browser, no localhost page.
    let status = await MusicAuthorization.request()
    emit(Result(ok: status == .authorized, status: describe(status),
                error: status == .authorized ? nil : "user did not grant access"))
}

/// Add one catalog item to the library. `kind` selects the `ids[...]` bucket:
/// songs and albums are different resources to Apple, and hardcoding `songs`
/// was why album adds still needed the developer token.
func cmdAdd(_ raw: String, kind: String) async -> Never {
    _ = requireAuthorization()
    guard let id = validCatalogID(raw) else { fail("catalog id must be numeric") }

    var components = URLComponents(string: "https://api.music.apple.com/v1/me/library")!
    components.queryItems = [URLQueryItem(name: "ids[\(kind)]", value: id)]
    var request = URLRequest(url: components.url!)
    request.httpMethod = "POST"
    // 202 Accepted is the documented success for a library add.
    await send(request, id: id)
}

/// Love or dislike a catalog song. Music.app's own AppleScript covers this for
/// tracks already IN the library; a catalog id has no library object to set it
/// on, which is what sent this down the developer-token path.
func cmdRate(_ raw: String, _ ratingRaw: String) async -> Never {
    _ = requireAuthorization()
    guard let id = validCatalogID(raw) else { fail("catalog id must be numeric") }
    let value: Int
    switch ratingRaw.lowercased() {
    case "love": value = 1
    case "dislike": value = -1
    default: fail("rating must be love or dislike")
    }

    let url = URL(string: "https://api.music.apple.com/v1/me/ratings/songs/\(id)")!
    var request = URLRequest(url: url)
    request.httpMethod = "PUT"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    let payload: [String: Any] = ["type": "rating", "attributes": ["value": value]]
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else {
        fail("could not encode the rating body")
    }
    request.httpBody = data
    await send(request, id: id)
}

/// Attach a catalog song to a library playlist. AppleScript can only edit
/// playlists Music.app itself owns, so an Apple-Music-origin playlist (the
/// `kind == "api"` case on the Python side) had no tokenless rail at all.
func cmdPlaylistAdd(_ playlistRaw: String, _ trackRaw: String, _ kind: String) async -> Never {
    _ = requireAuthorization()
    guard let playlistID = validPlaylistID(playlistRaw) else {
        fail("playlist id must look like p.XXXXXXXX")
    }
    guard let trackID = validTrackRef(trackRaw, kind: kind) else {
        fail("track id does not match kind \(kind)")
    }

    let url = URL(
        string: "https://api.music.apple.com/v1/me/library/playlists/\(playlistID)/tracks")!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    let payload: [String: Any] = ["data": [["id": trackID, "type": kind]]]
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else {
        fail("could not encode the playlist body")
    }
    request.httpBody = data
    await send(request, id: trackID)
}

/// List a library playlist's tracks, paging until Apple stops returning more.
///
/// This is the only verb that returns a `/v1/me/...` body on stdout, so it is
/// worth being explicit: that body goes to the parent process that spawned this
/// helper — same machine, same user, the boundary the unix socket already
/// assumes — and nowhere else. It exists because refusing to add a duplicate
/// requires knowing what is already in the playlist, and skipping that check
/// silently is how the AppleScript rail once stacked four copies of a track.
func cmdPlaylistTracks(_ playlistRaw: String) async -> Never {
    _ = requireAuthorization()
    guard let playlistID = validPlaylistID(playlistRaw) else {
        fail("playlist id must look like p.XXXXXXXX")
    }

    var collected: [Any] = []
    var offset = 0
    while true {
        var components = URLComponents(
            string: "https://api.music.apple.com/v1/me/library/playlists/"
                + "\(playlistID)/tracks")!
        components.queryItems = [
            URLQueryItem(name: "limit", value: "100"),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
        do {
            let response = try await MusicDataRequest(urlRequest: URLRequest(url: components.url!))
                .response()
            let code = response.urlResponse.statusCode
            // An empty playlist 404s rather than returning an empty list.
            if code == 404 { break }
            guard (200...299).contains(code) else {
                emit(Result(ok: false, httpStatus: code, error: "Apple returned \(code)"))
            }
            let parsed = try? JSONSerialization.jsonObject(with: response.data)
            let page = ((parsed as? [String: Any])?["data"] as? [Any]) ?? []
            collected.append(contentsOf: page)
            if page.count < 100 { break }
            offset += 100
            // A library playlist is bounded, but never trust a remote service
            // to terminate a loop for us.
            if offset >= 10_000 { break }
        } catch {
            fail(error.localizedDescription)
        }
    }

    guard let out = try? JSONSerialization.data(withJSONObject: ["data": collected]),
        let text = String(data: out, encoding: .utf8)
    else { fail("could not encode the playlist tracks") }
    emit(Result(ok: true, httpStatus: 200, body: text))
}

/// Resolve ISRCs to catalog songs. The only verb here that answers a question
/// the public iTunes Search API cannot answer at all, and the only READ verb —
/// batched, because Apple's filter takes a list and a process launch per track
/// would not be affordable.
func cmdISRC(_ raw: String) async -> Never {
    _ = requireAuthorization()
    guard let codes = validISRCList(raw) else {
        fail("each ISRC must be 12 alphanumeric characters")
    }
    // Ask MusicKit for the storefront rather than taking one as an argument:
    // it already knows which one this account belongs to, and a mismatched
    // storefront silently returns nothing rather than erroring.
    let storefront: String
    do {
        storefront = try await MusicDataRequest.currentCountryCode
    } catch {
        fail("could not determine the storefront: \(error.localizedDescription)")
    }

    var components = URLComponents(
        string: "https://api.music.apple.com/v1/catalog/\(storefront)/songs")!
    components.queryItems = [URLQueryItem(name: "filter[isrc]", value: codes)]
    let request = URLRequest(url: components.url!)
    await send(request, wantBody: true)
}

let args = Array(CommandLine.arguments.dropFirst())
guard let verb = args.first else {
    fail(
        "usage: amcp-musickit status|authorize|add|add-album|rate|playlist-add"
            + "|playlist-tracks|isrc")
}

switch verb {
case "status":
    cmdStatus()
case "authorize":
    await cmdAuthorize()
case "add":
    guard args.count == 2 else { fail("add needs exactly one catalog id") }
    await cmdAdd(args[1], kind: "songs")
case "add-album":
    guard args.count == 2 else { fail("add-album needs exactly one catalog id") }
    await cmdAdd(args[1], kind: "albums")
case "rate":
    guard args.count == 3 else { fail("rate needs a catalog id and love|dislike") }
    await cmdRate(args[1], args[2])
case "playlist-add":
    // The kind is optional and defaults to a catalog song, which is what every
    // caller wants; `library-songs` is for a track already in the library.
    guard args.count == 3 || args.count == 4 else {
        fail("playlist-add needs a playlist id, a track id, and an optional kind")
    }
    await cmdPlaylistAdd(args[1], args[2], args.count == 4 ? args[3] : "songs")
case "playlist-tracks":
    guard args.count == 2 else { fail("playlist-tracks needs exactly one playlist id") }
    await cmdPlaylistTracks(args[1])
case "isrc":
    guard args.count == 2 else { fail("isrc needs one comma-separated list") }
    await cmdISRC(args[1])
default:
    fail("unknown command \(verb)")
}
