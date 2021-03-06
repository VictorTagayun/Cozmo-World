import cozmo
import cozmo.event
import asyncio
import time
import threading
import random
from Patrol.Track.track import Track, BldgVertex
from cozmo.util import radians, degrees, distance_mm, speed_mmps
from cozmo.objects import CustomObjectMarkers, CustomObjectTypes
from cozmo.anim import Triggers

# time of updating frame
FRAME_DURATION = 0.08
# forward speed when Cozmo is unhappy
FORWARD_SPEED = 50
# forward speed when Cozmo is happy
FORWARD_SPEED_HAPPY = 100
# small number to round the caps
EPSILON = 0.00001

# approximate scale from real world distance to pixel numbers in vision
DISTANCE_TO_PIXEL_SCALE = 4.0

# mapping from color name to building id, note some building ids are switched intentionally
COLOR_TO_BLDG = {
    "Red": "BB",
    "Green": "GB",
    "Blue": "RB",
    "Yellow": "YB",
    "Magenta": "MB"
}
DELIVERY_UNIVERSE = [{"color": "Blue"},{"color": "Red"},{"color": "Green"},{"color": "Yellow"},{"color": "Magenta"}]
# normal (unhappy) autonomous delivery, at most once
MAX_DELIVERY = 0
# happy autonomous delivery, up to 6 times
MAX_DELIVERY_HAPPY = 5

# dropping bag reaction waiting time limit
# usually used when Cozmo is not near a marker
MAX_WAITING_TIME = 15.0
# animation triggers to 
ATTENTION_TRIGGERS = [cozmo.anim.Triggers.CantHandleTallStack, cozmo.anim.Triggers.CozmoSaysBadWord, cozmo.anim.Triggers.CubeMovedUpset, cozmo.anim.Triggers.FailedToRightFromFace, cozmo.anim.Triggers.GoToSleepGetOut]

class Patrol:
    def __init__(self, remote=None, robot=None):
        self.remote = remote
        self.track = Track()
        
        self.robot = robot

        # these two varaibles are not exact opposite
        # stopped: volatile variable, actually means whether autonomous is disabled
        self.stopped = False
        # started: whether autonomous motion is started, blocking duplicating calls of start()
        self.started = False
        
        # pose track, about the map data structure
        self.poseTrack = None
        # starting pose, about the real world
        self.initialPose = None
        
        # whether Cozmo is driving along the road, not turn to buildings on sides
        self.driveOnRoad = True
        
        # not used
        self.waitForOrder = False
        
        # wait for animation after dropping the bag
        self.waitForAnimation = False

        # distance fix parallel to the main road, depends on the vision
        self.offsetPixel = 0.0
        # whether offset updating is accepted, ignoring markers in vision in cirtain cases
        self.acceptOffset = False

        # variable related to self termination
        self.deliveryCount = 0
        self.attentionCount = 0

        # Cozmo's mood scale and mood related variables
        self.mood = -1
        self.forwardSpeed = FORWARD_SPEED
        self.maxDelivery = MAX_DELIVERY

        if remote:
            remote.cozmo.world.add_event_handler(cozmo.objects.EvtObjectAppeared, self.onMarkerSeen)
        
    # entrance of cozmo connection if directly run in main function
    async def run(self, coz_conn: cozmo.conn.CozmoConnection):
        robot = await coz_conn.wait_for_robot()

        if not self.remote:
            await self.defineCustomObjects(robot.world)

        robot.world.add_event_handler(cozmo.objects.EvtObjectObserved, self.onMarkerSeen)
        
        await self.start(robot)

    # start autonomous
    async def start(self, robot: cozmo.robot.Robot):
        if self.started or self.stopped:
            return
        self.started = True
        self.robot = robot

        # set mood dependent variables
        if self.mood >= 0:
            self.forwardSpeed = FORWARD_SPEED_HAPPY
            self.maxDelivery = MAX_DELIVERY_HAPPY

        self.robot.abort_all_actions();
        self.robot.stop_all_motors();

        # lift height not be 1, because the height of garage gate
        await robot.set_lift_height(0.8).wait_for_completed()
        await robot.set_head_angle(degrees(30)).wait_for_completed()

        # move out of garage home and turn to the road
        # TODO: this distance is likely to be inaccurate
        await robot.drive_straight(distance_mm(140), speed_mmps(self.forwardSpeed)).wait_for_completed()
        await robot.turn_in_place(degrees(-90)).wait_for_completed()
        
        # await self.loop(robot)
        await self.loopPath(robot)
        # await self.searchForCustomObject(robot)

    # turn around and search for any custom object
    async def searchForCustomObject(self, robot: cozmo.robot.Robot):
        lookAround = robot.start_behavior(cozmo.behavior.BehaviorTypes.LookAroundInPlace)

        obj = None

        try:
            obj = await self.waitForObservedCustomObject(robot, timeout=30)
            print("Found custom obj: %s" % obj)
        except asyncio.TimeoutError:
            print("Didn't find a custom object")
        finally:
            lookAround.stop()

        if obj:
            pass
            #await robot.go_to_object(obj)
    
    # 
    async def waitForObservedCustomObject(self, robot: cozmo.robot.Robot, timeout=None):
        filter = cozmo.event.Filter(cozmo.objects.EvtObjectObserved,
                                    obj=lambda obj: isinstance(obj, cozmo.objects.CustomObject))
        evt = await robot.world.wait_for(filter, timeout=timeout)
        return evt.obj

    async def greetToMarker(self, robot: cozmo.robot.Robot):
        # stop
        robot.stop_all_motors()
        # record pose
        pose = robot.pose
        # turn to side
        await robot.turn_in_place(degrees(-90)).wait_for_completed()
        # greet
        await robot.play_anim_trigger(Triggers.NamedFaceInitialGreeting).wait_for_completed()
        # turn back
        await robot.go_to_pose(pose).wait_for_completed()
        # resume moving
        await robot.drive_wheels(self.forwardSpeed, self.forwardSpeed)

    async def loop(self, robot: cozmo.robot.Robot):
        if robot.is_on_charger:
            await robot.drive_off_charger_contacts().wait_for_completed()

        self.initialPose = robot.pose
        self.poseTrack = self.track.getPoseTrack(self.forwardSpeed)
        self.driveOnRoad = True
        self.stopped = False

        await robot.drive_wheels(self.forwardSpeed, self.forwardSpeed)
        print(self.poseTrack.edge.end.id)
        
        while not self.stopped:

            # basic update of straight drive
            # picking next edge if the current is finished
            if self.driveOnRoad:
                self.poseTrack.update(FRAME_DURATION, self.forwardSpeed)

            # back to starting point
            if self.poseTrack.consumeRouteEndSignal():
                self.driveOnRoad = False
                # stop before turn
                robot.stop_all_motors()
                await robot.go_to_pose(self.initialPose).wait_for_completed()
                await robot.drive_wheels(self.forwardSpeed, self.forwardSpeed)
                # turning is included in go to pose
                self.poseTrack.consumeEdgeChangeSignal()
                self.driveOnRoad = True
                
                print("fix position at initial pose")
                print(self.poseTrack.edge.end.id)
            # at any other cross or corner
            elif self.poseTrack.consumeEdgeChangeSignal():
                self.driveOnRoad = False
                diff = self.poseTrack.angleDiff
                angleAbs = self.poseTrack.angleAbs
                # make sure the picked next
                if diff < -0.1 or diff > 0.1:
                    # stop before turn
                    robot.stop_all_motors()
                    # make a turn
                    angle = radians(angleAbs + self.initialPose.rotation.angle_z.radians - robot.pose.rotation.angle_z.radians)
                    await robot.turn_in_place(angle).wait_for_completed()
                    print("make a turn")
                    # restart motion
                    await robot.drive_wheels(self.forwardSpeed, self.forwardSpeed)
                self.driveOnRoad = True
                
                print(self.poseTrack.edge.end.id)

            # let Cozmo drive straight for a short time before updates
            await asyncio.sleep(FRAME_DURATION)

        robot.stop_all_motors()

    async def loopPath(self, robot: cozmo.robot.Robot):
        if robot.is_on_charger:
            await robot.drive_off_charger_contacts().wait_for_completed()

        self.initialPose = robot.pose
        self.pathPoseTrack = self.track.getPathPoseTrack(self.forwardSpeed)
        self.driveOnRoad = True
        self.stopped = False

        self.waitForOrder = False
        
        self.waitForAnimation = False
        self.offsetPixel = 0.0

        self.deliveryCount = 0
        self.attentionCount = 0

        print("start drive")
##        await robot.drive_wheels(FORWARD_SPEED, FORWARD_SPEED)
        print(self.pathPoseTrack.distance)
        await robot.drive_straight(distance_mm(self.pathPoseTrack.distance), speed_mmps(self.forwardSpeed)).wait_for_completed()
        print(self.pathPoseTrack.edge.end.id)
        
        while not self.stopped:

            # basic update of straight drive
            # picking next edge if the current is finished
            if self.driveOnRoad:
##                self.pathPoseTrack.update(FRAME_DURATION, FORWARD_SPEED)
                # finish this path, because drive_straight is used and waited to finish
                self.pathPoseTrack.update(999, self.forwardSpeed)

            # did the last auto delivery
            if self.deliveryCount > self.maxDelivery:
                await robot.play_anim_trigger(random.choice(ATTENTION_TRIGGERS)).wait_for_completed()
                self.attentionCount = self.attentionCount + 1
            # end of the path
            elif self.pathPoseTrack.consumeRouteEndSignal():
                self.driveOnRoad = False
                # stop before turn
                robot.stop_all_motors()
                # turning is included in go to pose
                self.pathPoseTrack.consumeEdgeChangeSignal()

                # go to loading area and drop cube
                # lift cube, go back to road
                bldgId = self.pathPoseTrack.edge.start.id
                if bldgId != "GA":
                    await self.deliverItem(robot, self.pathPoseTrack.edge.start, self.pathPoseTrack.path.lastTurnRight)
                else:
                    await self.backInGarage(robot, False)
                    self.stopped = True

                if self.stopped:
                    break

                # continue auto delivery
                if self.deliveryCount <= self.maxDelivery:
                    await self.depart(robot)

                print("Move towards: %s" % self.pathPoseTrack.edge.end.id)
                
            # at any road intersection
            elif self.pathPoseTrack.consumeEdgeChangeSignal():
                self.driveOnRoad = False
                diff = self.pathPoseTrack.angleDiff
                angleAbs = self.pathPoseTrack.angleAbs
                # a turn is about to be made
                if diff < -0.1 or diff > 0.1:
                    # stop before turn
                    robot.stop_all_motors()
                    # make a turn
                    angle = radians(angleAbs + self.initialPose.rotation.angle_z.radians - robot.pose.rotation.angle_z.radians)
                    await robot.turn_in_place(angle).wait_for_completed()
                    print("turn of angle: ", angle.degrees)
                    # restart motion
##                    await robot.drive_wheels(FORWARD_SPEED, FORWARD_SPEED)

                anim_done = False;
                while anim_done is False:
                    try:
                        await robot.drive_straight(distance_mm(self.pathPoseTrack.distance), speed_mmps(self.forwardSpeed)).wait_for_completed()
                        anim_done = True
                    except cozmo.exceptions.RobotBusy:
                        await asyncio.sleep(0.1);

                self.driveOnRoad = True
                
                print("Move towards: %s" % self.pathPoseTrack.edge.end.id)

            # let Cozmo drive straight for a short time before updates
            await asyncio.sleep(FRAME_DURATION)

        robot.stop_all_motors()

    async def depart(self, robot: cozmo.robot.Robot):
        await robot.drive_straight(distance_mm(self.pathPoseTrack.distance), speed_mmps(self.forwardSpeed)).wait_for_completed()    
        self.driveOnRoad = True
        self.waitForOrder = False

    def findAndUpdatePath(self, startId, endId, nextId):
        path = self.track.getPath(startId, endId, nextId)
        # offset = -(self.offsetPixel / DISTANCE_TO_PIXEL_SCALE)
        self.pathPoseTrack.updatePath(path, self.forwardSpeed)

    # compute destination of next path based on state of bag and current building
    async def computeDestId(self, bldgId: str, robot: cozmo.robot.Robot):
        deliveryBag = []
        if self.remote:
            # access from remote controller of lights currently on
            deliveryBag = self.remote.lights_on
        else:
            # Mock the colors in bag-----------------
            if bldgId == "PH":
                deliveryBag.append(random.choice(DELIVERY_UNIVERSE))
                pass
            elif bldgId == "RB":
                # deliveryBag.append({"color":"Blue"})
                pass
            # mock finish-------------------------------

        destId = None
        colorName = next((n["color"] for n in deliveryBag if n is not None), None)
        print("Color: %s" % colorName)
        
        # bag contains some pizza
        if colorName:
            destId = COLOR_TO_BLDG[colorName]
        # bas is empty
        else:
            if robot.battery_voltage < 3.5:
                # low voltage, back to home
                destId = "GA"
            elif bldgId == "PH":
                # Start waiting for pizza
                self.waitForOrder = True
            elif bldgId == "GA":
                # Back to home, take rest
                await self.backInGarage(robot, False)
            else:
                # empty bag, return pizza shop to get more
                destId = "PH"

        # pizza buyers' buildings
        if bldgId != "GA" and bldgId != "PH":
            self.deliveryCount = self.deliveryCount + 1

        return destId

    # Do the delivery or pick up
    async def deliverItem(self, robot: cozmo.robot.Robot, bldg: BldgVertex, destTurnRight=True):
        pose = robot.pose
        # turn to the lane
        await robot.turn_in_place(degrees(-90 * self.flagToScale(destTurnRight))).wait_for_completed()
        # approach the marker
        await robot.drive_straight(distance_mm(bldg.d), speed_mmps(self.forwardSpeed / 2)).wait_for_completed()
        
        # protected action
        anim_done = False;
        while anim_done is False:
            try:
                await robot.set_head_angle(degrees(30)).wait_for_completed();
                anim_done = True
            except cozmo.exceptions.RobotBusy:
                await asyncio.sleep(0.1);

        self.waitForAnimation = True
        # open offset window
        self.acceptOffset = True
        # drop bag
        await robot.set_lift_height(0.0 + EPSILON).wait_for_completed()
        await asyncio.sleep(0.1)
        self.acceptOffset = False

        print("Start waiting for animation")
        if self.remote:
            waitingTime = 0.0
            while self.waitForAnimation:
                await asyncio.sleep(0.1)
                # autonomous turned off
                if self.stopped:
                    return
                # wait too long, have trouble, attract human attention for help
                if waitingTime > MAX_WAITING_TIME:
                    self.deliveryCount = 999
                    return
                waitingTime = waitingTime + 0.1
        # remote controller not present, mock the behavior
        else:
            await asyncio.sleep(5)
        print("Finish waiting for animation")

        bldgId = self.pathPoseTrack.edge.start.id
        # sad reaction before picking up cube
        if bldgId == "PH" and self.mood < 0:
            await robot.play_anim_trigger(cozmo.anim.Triggers.FrustratedByFailureMajor).wait_for_completed()
            await asyncio.sleep(1);

        await robot.set_lift_height(1.0 - EPSILON).wait_for_completed()

        # parameters for path finding
        nextId = self.pathPoseTrack.edge.end.id
        destId = await self.computeDestId(bldgId, robot)
        # find path, update to pathPoseTrack
        self.findAndUpdatePath(bldgId, destId, nextId)
        initTurnLeft = self.pathPoseTrack.path.firstTurnLeft


        # back to road
        await robot.drive_straight(distance_mm(-bldg.d), speed_mmps(self.forwardSpeed / 2)).wait_for_completed()
        
        # delivery count not at max, drive normally
        if self.deliveryCount <= self.maxDelivery:
            await robot.turn_in_place(degrees(90 * self.flagToScale(initTurnLeft))).wait_for_completed()
            offset = -(self.offsetPixel / DISTANCE_TO_PIXEL_SCALE)
            if abs(offset) < 200:
                self.pathPoseTrack.updateOffset(offset * self.flagToScale(initTurnLeft))
        # the very first attention reaction
        elif self.attentionCount == 0 and self.mood < 0:
            await self.remote.stopSadMusic()
            await robot.turn_in_place(degrees(180)).wait_for_completed()
            await robot.say_text("I don't want to work").wait_for_completed()
        
    # turn and drive backwards to return to garage
    async def backInGarage(self, robot: cozmo.robot.Robot, ccrflag: bool):
        await robot.turn_in_place(degrees(90 * self.flagToScale(ccrflag))).wait_for_completed()
        await robot.drive_wheels(-self.forwardSpeed / 2, -self.forwardSpeed / 2)
        await asyncio.sleep(2)
        robot.stop_all_motors()

    # change Cozmo's mood
    def change_mood(self, value):
        self.mood = value;

    # disable autonomous mode. terminate autonomous execution if running
    def disableAuto(self):
        if not self.stopped:
            self.stopped = True
            self.started = False
            self.robot.abort_all_actions()
            self.robot.set_head_angle(cozmo.util.Angle(degrees=30));

    # enable autonomous mode. Need to start by calling start() explicitly
    def enableAuto(self):
        if self.stopped:
            self.stopped = False

    # binary scale
    def flagToScale(self, flag: bool):
        scale = 1
        if flag == False:
            scale = -1
        return scale

    def onMarkerSeen(self, evt: cozmo.objects.EvtObjectObserved, image_box=None, obj=None, pose=None, **kwargs):
        if self.acceptOffset and isinstance(obj, cozmo.objects.CustomObject):
            self.offsetPixel = image_box.top_left_x + image_box.width * 0.5 - 160
            print("custom marker offset in pixels: ", self.offsetPixel)

    async def onReactiveAnimationFinished(self):
        print("ANIMATION FINISHED");
        self.waitForAnimation = False

    # define custom objects. Is called only when remote controller not present
    async def defineCustomObjects(self, world):
        cube_obj_1 = await world.define_custom_cube(CustomObjectTypes.CustomType02,
                                                  CustomObjectMarkers.Diamonds2,
                                                  100,
                                                  90, 90, False)
        cube_obj_2 = await world.define_custom_cube(CustomObjectTypes.CustomType03,
                                                             CustomObjectMarkers.Diamonds3,
                                                             100,
                                                             90, 90, True)
        cube_obj_3 = await world.define_custom_cube(CustomObjectTypes.CustomType04,
                                                             CustomObjectMarkers.Diamonds4,
                                                             100,
                                                             90, 90, True)
        cube_obj_4 = await world.define_custom_cube(CustomObjectTypes.CustomType05,
                                                             CustomObjectMarkers.Diamonds5,
                                                             100,
                                                             90, 90, True)

        cube_obj_5 = await world.define_custom_cube(CustomObjectTypes.CustomType06,
                                                             CustomObjectMarkers.Circles2,
                                                             100,
                                                             90, 90, True)
        cube_obj_6 = await world.define_custom_cube(CustomObjectTypes.CustomType07,
                                                             CustomObjectMarkers.Circles3,
                                                             100,
                                                             90, 90, True)
        cube_obj_7 = await world.define_custom_cube(CustomObjectTypes.CustomType08,
                                                             CustomObjectMarkers.Circles4,
                                                             100,
                                                             90, 90, True)
        cube_obj_8 = await world.define_custom_cube(CustomObjectTypes.CustomType09,
                                                             CustomObjectMarkers.Circles5,
                                                             100,
                                                             90, 90, True)

        cube_obj_9 = await world.define_custom_cube(CustomObjectTypes.CustomType10,
                                                             CustomObjectMarkers.Triangles2,
                                                             100,
                                                             90, 90, True)
        cube_obj_10 = await world.define_custom_cube(CustomObjectTypes.CustomType11,
                                                             CustomObjectMarkers.Triangles3,
                                                             100,
                                                             90, 90, True)
        cube_obj_11 = await world.define_custom_cube(CustomObjectTypes.CustomType12,
                                                             CustomObjectMarkers.Triangles4,
                                                             100,
                                                             90, 90, True)
        cube_obj_12 = await world.define_custom_cube(CustomObjectTypes.CustomType13,
                                                             CustomObjectMarkers.Triangles5,
                                                             100,
                                                             90, 90, True)

        cube_obj_13 = await world.define_custom_cube(CustomObjectTypes.CustomType14,
                                                             CustomObjectMarkers.Hexagons2,
                                                             100,
                                                             90, 90, True)
        cube_obj_14 = await world.define_custom_cube(CustomObjectTypes.CustomType15,
                                                             CustomObjectMarkers.Hexagons3,
                                                             100,
                                                             90, 90, True)
        cube_obj_15 = await world.define_custom_cube(CustomObjectTypes.CustomType16,
                                                             CustomObjectMarkers.Hexagons4,
                                                             100,
                                                             90, 90, True)
        cube_obj_16 = await world.define_custom_cube(CustomObjectTypes.CustomType17,
                                                             CustomObjectMarkers.Hexagons5,
                                                             100,
                                                             90, 90, True)

def main():
    p = Patrol()
    cozmo.setup_basic_logging()
    cozmo.connect_with_tkviewer(p.run)

if __name__ == "__main__":
    main()
    
