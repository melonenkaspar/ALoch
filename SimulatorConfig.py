"""
Control Room for the project in itself.

Configurations and miscellaneous manual control settings will be explained here:
#1 - Standard controls
#2 - Behavior control
    #2-1 references which moves are executed
        Valid Types:
            random
            engine
            hybrid (not implemented yet) #requires further config, won't be applied when not active
    #2-2 references how trades are done
        Valid Types:
            random
            engine
            strongestDeviation #Prefered the highest trades, gets rid of lowest
            hybrid (not implemented yet) #requires further config, won't be applied when not active
#3 - Describes the card deck size (Pokerreihenfolge)
    Valid Types:
        half # 7 to ace
        full # 2 to ace

Deck description:
    2, 3, 4, 5, 6, 7, 8, 9, 10, Jack, Queen, King, Ace
    clubs (♣), diamonds (♦), hearts (♥) and spades (♠)
    capitalized letter or number for base, decapitalized letter for variation

    Examples:   2c -> 2 of clubs
                10s -> 10 of spades

    Computational order:

    000000 2c 0  2
    000001 2d 1  2
    000010 2h 2  2
    000011 2s 3  2
    000100 3c 4  3
    000101 3d 5  3
    000110 3h 6  3
    000111 3s 7  3
    001000 4c 8  4
    001001 4d 9  4
    001010 4h 10 4
    001011 4s 11 4


    plays:
    Plays are compiled in the following format:

    [rank]x[count]
    playing a double 2 is compiled as:
    2x2
    -> Idea is so that getRank() works on both the card and the play

initial Author: MrPandaMaan
last modification date: 5/26/2026
"""

#1    General Simulation Controls
gameIterations = 1    #how many individual games are run (1-inf)
gameLength = 3    #chooses how many rounds a single game is played (1-inf)
gameEntities = 5    #count of players (2-inf, default 5)


#2    Behavior control
#2-1 moves
behaviorTypeMoves = 'random'
#2-2 trading
behaviorTypeTrading = 'random'

#Decides whether the possibleMoves() list should include a native 'Pass'
enablePass = True



#3    Deck
rankOrder = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]
deckSize = 'full'

#4    Ranks
gameRanks = ["President", "Vice-President", "Citizen", "Vice-Brokie", "Brokie"]
