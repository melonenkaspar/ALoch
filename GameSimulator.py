
import random

import SimulatorConfig
import SimulatorFunctions
from SimulatorFunctions import *
from SimulatorConfig import *

def main():
    citizenDeck = []
    stack = []
    possibleMoveList = []
    discardStack = []

    deck = createDeck()
    players = createEntities()
    shuffleDeck(deck)
    print(deck)

    dealHands(deck, players, citizenDeck)
    projectConsole(players, citizenDeck, stack, discardStack)
    possibleMoveList = possibleMoves(players, 0, "7x2")
    print(possibleMoveList)

    if possibleMoveList:
        print("Played a move:", possibleMoveList[0])
        play(players, 0, stack, possibleMoveList[0])
        projectConsole(players, citizenDeck, stack, discardStack)

def devTest():
    x = range(5)
    print(x)

def runGame():
    citizenDeck = []; stack = []; discardStack = []; mostRecentMove = None; gameEnd = False
    trickOrder = []; passedCounter = 0; lengthOfGame = 0
    remainingRanks = copy.deepcopy(SimulatorConfig.gameRanks)



    deck = createDeck(); players = createEntities() #2 new vars
    shuffleDeck(deck)
    dealHands(deck, players, citizenDeck)

    for entityID in range(len(players)):
        trickOrder.append(entityID)
    random.shuffle(trickOrder)
    nextTrickOrder = copy.deepcopy(trickOrder) #1 new var

    printSeparator()
    print(f'Initiating game w/ initial trickOrder = {trickOrder} and state:')
    projectConsole(players, citizenDeck, stack, discardStack)
    printSeparator()


    ''''''
    while len(discardStack) < (52 - len(citizenDeck)) and not gameEnd:
        trickOrder = copy.deepcopy(nextTrickOrder)
        for trick in trickOrder:
            lengthOfGame += 1
            printSeparator()
            move = randomMove(possibleMoves(players, trick, mostRecentMove))
            if move == 'pass':
                print(f'Player {trick} passed the trick. PassedMoves: {passedCounter + 1}')
                passedCounter += 1
                pass
            else:
                play(players, trick, stack, move); mostRecentMove = move; passedCounter = 0
                print(f'Player {trick} played a move: {mostRecentMove}')
                projectConsole(players, citizenDeck, stack, discardStack)

                if not players[trick]['hand']:
                    if remainingRanks:
                        #Rank assortment
                        if getRank(mostRecentMove) == 'A':
                            players[trick]['rank'] = remainingRanks[len(remainingRanks) - 1]
                            del remainingRanks[len(remainingRanks) - 1]
                        else:
                            players[trick]['rank'] = remainingRanks[0]
                            del remainingRanks[0]

                        #TrickOrder modification [removal of winner]
                        trickIndexWin = trickOrder.index(trick)
                        trickOrder.remove(trick); nextTrickOrder.remove(trick)

                        if len(remainingRanks) == 1:
                            players[trickOrder[0]]['rank'] = remainingRanks[0]
                            del remainingRanks[0]

                        #Compute new trick [next player in order]
                        trick = trickOrder[trickIndexWin % len(trickOrder)]
                        nextTrickOrder = [trick]
                        trickIndex = trickOrder.index(trick) #1 new var
                        for entity in range(len(trickOrder) - 1):
                            nextTrickOrder.append(trickOrder[(entity + trickIndex + 1) % len(trickOrder)])
            if passedCounter >= len(remainingRanks):
                #Prepare trickOrder for next round
                nextTrickOrder = [trick]
                trickIndex = trickOrder.index(trick) #1 new var
                for entity in range(len(trickOrder) - 1):
                    nextTrickOrder.append(trickOrder[(entity + trickIndex + 1) % len(trickOrder)])

                #Reset shit for next round
                passedCounter = 0
                for element in stack:
                    discardStack.append(element)
                mostRecentMove = None; stack.clear()
                break

    ''''''
    printSeparator()
    printSeparator()
    print('Game has been simulated')
    #print(f'All players: {players}')
    projectConsole(players, citizenDeck, stack, discardStack)
    print(len(discardStack))
    print(f'Current game Length: {lengthOfGame} iterations')
    printSeparator()

if __name__ == "__main__":
    runGame()
