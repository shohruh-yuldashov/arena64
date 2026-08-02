"""`game`'s application layer — A64-015.4.

The layer A64-014.6 recorded as absent: "There is also no `application/`,
`infrastructure/` or `presentation/` layer. This task is pure domain."
Match persistence is what earns the first two.

    ports.py     `MatchRecordRepository`, declared by the layer that needs
                 it (AD-06)
    services/    the four use cases behind `game.public`

There is still no `presentation/`, and there is deliberately no router:
every endpoint that touches a match today is a *matchmaking* endpoint,
because a player who has not accepted yet is still being matched. `game`
gains routes when there is a game to play.
"""
